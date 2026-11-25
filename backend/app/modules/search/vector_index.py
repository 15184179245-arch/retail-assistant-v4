"""Versioned SQLite sidecar vectors; the authoritative catalog remains read-only."""
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

from sqlalchemy import and_,select,func
from backend.app.modules.catalog.models import CatalogProduct,CatalogSnapshot
from backend.app.modules.search.refinement import canonical_text,matches_term

MAX_DOCUMENTS=100_000


class VectorUnavailable(ValueError):
    pass


def catalog_documents(session,organization_id,store_id):
    p=CatalogSnapshot.payload
    statement=select(CatalogProduct.id,CatalogSnapshot.id,CatalogSnapshot.payload_sha256,
        CatalogProduct.external_code,*(func.coalesce(p[k].as_string(),p[('raw',k)].as_string(),p[('raw','raw',k)].as_string())
            for k in ('name','spec','brandName','productCategoryIdName','model'))).join(
        CatalogSnapshot,and_(CatalogSnapshot.id==CatalogProduct.current_snapshot_id,
        CatalogSnapshot.product_id==CatalogProduct.id,CatalogSnapshot.organization_id==CatalogProduct.organization_id,
        CatalogSnapshot.store_id==CatalogProduct.store_id)).where(CatalogProduct.organization_id==organization_id,
        CatalogProduct.store_id==store_id,CatalogProduct.active.is_(True)).order_by(CatalogProduct.id).limit(MAX_DOCUMENTS+1)
    docs=[];digest=sha256()
    for identity,snapshot,source_sha,code,name,spec,brand,category,model in session.execute(statement):
        if len(docs)>=MAX_DOCUMENTS:raise VectorUnavailable('vector_catalog_over_limit')
        text='；'.join(str(v) for v in (name,spec,brand,category,model) if isinstance(v,str) and v)
        row=dict(id=identity,snapshot=snapshot,source_sha=source_sha,text=text,
                 spec_text=canonical_text('\n'.join(v for v in (code,name,spec,model) if isinstance(v,str))))
        docs.append(row);digest.update(json.dumps(row,ensure_ascii=False,sort_keys=True).encode())
    return docs,digest.hexdigest()


def build_index(session,*,organization_id,store_id,embedder,target,batch_size=32,previous_index=None):
    import numpy as np
    target=Path(target).resolve()
    if target.exists() or not 1<=batch_size<=64:raise VectorUnavailable('vector_new_target_required')
    docs,fingerprint=catalog_documents(session,organization_id,store_id)
    if not docs:raise VectorUnavailable('vector_catalog_empty')
    database=session.get_bind().url.database
    if database and str(database)!=':memory:' and Path(database).resolve()==target:
        raise VectorUnavailable('vector_source_target_denied')
    previous={};dimensions=None
    if previous_index is not None:
        old=Path(previous_index).resolve()
        if not old.is_file():raise VectorUnavailable('vector_previous_index_missing')
        source=sqlite3.connect(old.as_uri()+'?mode=ro',uri=True)
        try:
            source.execute('PRAGMA query_only=ON')
            meta={k:json.loads(v) for k,v in source.execute('SELECT key,value FROM metadata')}
            if (meta.get('organization_id')!=organization_id or meta.get('store_id')!=store_id or
                    meta.get('model')!=embedder.identity or meta.get('schema_version') not in (1,2)):
                raise VectorUnavailable('vector_previous_index_mismatch')
            # v1 lacks document SHA: it remains readable but cannot be reused.
            if meta['schema_version']==2:
                dimensions=meta['dimensions']
                for identity,digest,blob in source.execute('SELECT id,document_sha,vector FROM vectors LIMIT ?',
                                                          (MAX_DOCUMENTS+1,)):
                    if len(previous)>=MAX_DOCUMENTS:raise VectorUnavailable('vector_previous_index_over_limit')
                    vector=np.frombuffer(blob,dtype='<f4')
                    if len(vector)!=dimensions or not np.isfinite(vector).all():
                        raise VectorUnavailable('vector_previous_index_invalid')
                    previous[identity]=(digest,blob)
        except (sqlite3.Error,ValueError,KeyError):
            raise VectorUnavailable('vector_previous_index_invalid') from None
        finally:source.close()
    descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(descriptor)
    connection=sqlite3.connect(target);encoded=0;reused=0
    try:
        connection.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        connection.execute('CREATE TABLE vectors (id TEXT PRIMARY KEY,snapshot TEXT NOT NULL,spec_text TEXT NOT NULL,document_sha TEXT NOT NULL,vector BLOB NOT NULL)')
        pending=[]
        def save(doc,digest,blob):
            connection.execute('INSERT INTO vectors VALUES (?,?,?,?,?)',
                (doc['id'],doc['snapshot'],doc['spec_text'],digest,blob))
        for doc in docs:
            digest=sha256(doc['text'].encode()).hexdigest();old=previous.get(doc['id'])
            if old and old[0]==digest:
                save(doc,digest,old[1]);reused+=1
            else:pending.append((doc,digest))
        for offset in range(0,len(pending),batch_size):
            batch=pending[offset:offset+batch_size]
            matrix=embedder.encode([doc['text'] for doc,_ in batch])
            if matrix.ndim!=2 or len(matrix)!=len(batch) or not np.isfinite(matrix).all():
                raise VectorUnavailable('vector_embedding_invalid')
            if dimensions is None:dimensions=matrix.shape[1]
            if dimensions!=matrix.shape[1]:raise VectorUnavailable('vector_dimension_mismatch')
            for (doc,digest),vector in zip(batch,matrix):
                norm=np.linalg.norm(vector)
                if norm<=0:raise VectorUnavailable('vector_embedding_invalid')
                value=np.asarray(vector/norm,dtype='<f4')
                save(doc,digest,value.tobytes());encoded+=1
        meta=dict(schema_version=2,organization_id=organization_id,store_id=store_id,
                  model=embedder.identity,source_fingerprint=fingerprint,count=len(docs),dimensions=dimensions,
                  reused_vectors=reused,encoded_vectors=encoded)
        connection.executemany('INSERT INTO metadata VALUES (?,?)',[(k,json.dumps(v)) for k,v in meta.items()])
        connection.commit();target.chmod(0o600)
        return meta
    except Exception:
        connection.rollback()
        raise
    finally:connection.close()


def vector_search(session,*,organization_id,store_id,embedder,index_path,query,spec_terms=(),limit=40):
    import numpy as np
    path=Path(index_path).resolve()
    if not path.is_file() or not 1<=limit<=100:raise VectorUnavailable('vector_index_unavailable')
    docs,fingerprint=catalog_documents(session,organization_id,store_id)
    connection=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    connection.execute('PRAGMA query_only=ON')
    try:
        meta={k:json.loads(v) for k,v in connection.execute('SELECT key,value FROM metadata')}
        if meta.get('schema_version') not in (1,2) or meta.get('organization_id')!=organization_id or meta.get('store_id')!=store_id:
            raise VectorUnavailable('vector_scope_mismatch')
        if meta.get('model')!=embedder.identity:raise VectorUnavailable('vector_model_mismatch')
        if meta.get('source_fingerprint')!=fingerprint:raise VectorUnavailable('vector_index_stale')
        current={d['id']:d['snapshot'] for d in docs}
        rows=[(i,s,b) for i,s,text,b in connection.execute('SELECT id,snapshot,spec_text,vector FROM vectors ORDER BY id')
              if i in current and s==current[i] and all(matches_term(term,text) for term in spec_terms)]
        if not rows:return []
        matrix=np.asarray([np.frombuffer(b,dtype='<f4') for _,_,b in rows],dtype='float32')
        if matrix.shape[1]!=meta.get('dimensions') or not np.isfinite(matrix).all():
            raise VectorUnavailable('vector_index_invalid')
        vector=embedder.encode([query],query=True)[0]
        if vector.shape[0]!=matrix.shape[1]:raise VectorUnavailable('vector_dimension_mismatch')
        scores=matrix@vector
        # Exact cosine comparison across this catalog, no approximate ANN pruning.
        ranked=np.argsort(-scores,kind='stable')[:limit]
        return [dict(product_id=rows[int(n)][0],snapshot_id=rows[int(n)][1],score=float(scores[n]))
                for n in ranked if scores[n]>=0.55]
    except VectorUnavailable:raise
    except (sqlite3.Error,ValueError,KeyError,TypeError):
        raise VectorUnavailable('vector_index_invalid') from None
    finally:connection.close()
