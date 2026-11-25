"""Immutable source-field/derived-clue lookup. Never rewrites authoritative facts."""
from hashlib import sha256
import json,os,sqlite3,re
from pathlib import Path

from sqlalchemy import and_,select,text
from sqlalchemy.exc import DBAPIError
from backend.app.modules.catalog.models import CatalogProduct,CatalogSnapshot
from backend.app.modules.catalog import repository

MAX_PRODUCTS=100_000
MAX_RESULTS=1000
_WIDTH_MAP={n:n-0xFEE0 for n in range(0xFF01,0xFF5F)}
_SIZE_SEPARATOR=re.compile(r'(?<=\d)\s*[×*ｘx]\s*(?=\d)')


def source_refs(session,org,store):
    statement=select(CatalogProduct.id,CatalogSnapshot.id,CatalogSnapshot.payload_sha256).join(
        CatalogSnapshot,and_(CatalogSnapshot.id==CatalogProduct.current_snapshot_id,
            CatalogSnapshot.product_id==CatalogProduct.id,CatalogSnapshot.organization_id==CatalogProduct.organization_id,
            CatalogSnapshot.store_id==CatalogProduct.store_id)).where(CatalogProduct.organization_id==org,
        CatalogProduct.store_id==store,CatalogProduct.active.is_(True)).order_by(CatalogProduct.id).limit(MAX_PRODUCTS+1)
    rows=[dict(product_id=i,snapshot_id=s,payload_sha256=h) for i,s,h in session.execute(statement)]
    if len(rows)>MAX_PRODUCTS:raise ValueError('source_clue_limit')
    digest=sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return rows,digest



def current_fingerprint(session,org,store):
    """SQLite connection-local cache invalidated by both own and other writes.

    data_version values cannot be compared across connections, so cached values
    live on SQLAlchemy's physical connection info. total_changes catches writes
    on this same connection (including rolled-back writes, safely conservative).
    Other dialects retain the full scoped source check. No TTL or max timestamp.
    """
    connection=session.connection()
    if connection.dialect.name!='sqlite':
        return source_refs(session,org,store)[1]
    # total_changes does not decrease after rollback. Never cache an
    # uncommitted SQLite snapshot, or its fingerprint could survive rollback.
    if connection.connection.driver_connection.in_transaction:
        return source_refs(session,org,store)[1]
    def stamp():
        return tuple(connection.execute(text(
            'SELECT total_changes(),data_version FROM pragma_data_version')).one())
    try:
        before=stamp()
    except DBAPIError:
        return source_refs(session,org,store)[1]
    cache=connection.info.setdefault('source_clue_fingerprints',{})
    key=(org,store)
    cached=cache.get(key)
    if cached and cached[0]==before:return cached[1]
    fingerprint=source_refs(session,org,store)[1]
    after=stamp()
    if after==before:
        if len(cache)>=8:cache.clear()
        cache[key]=(after,fingerprint)
    return fingerprint


def comparison_text(value):
    """Safe comparison only: ASCII width, case and numeric size separators.

    Do not apply NFKC, remove digits, expand exponents, or convert units.
    """
    value=value.translate(_WIDTH_MAP).casefold().strip()
    return _SIZE_SEPARATOR.sub('x',value)


def contains_term(query,value):
    if not query:return False
    if not re.search(r'\d',query):return query in value
    left=r'(?<![\d.])' if query[0].isdigit() else r'(?<![A-Za-z0-9_.])'
    right=r'(?![\d.])' if query[-1].isdigit() else ''
    return re.search(left+re.escape(query)+right,value) is not None


def _fields(payload):
    # Explicit paths and strings only. Do not import supplier/price/stock/people.
    names=('code','name','shortName','spec','model','brandName','barCode','pyCode','productCategoryIdName')
    result={}
    for prefix in ('','raw.','raw.raw.'):
        obj=payload
        for key in prefix.rstrip('.').split('.') if prefix else []:
            obj=obj.get(key,{}) if isinstance(obj,dict) else {}
        if isinstance(obj,dict):
            for name in names:
                value=obj.get(name)
                if isinstance(value,str) and value:result[prefix+name]=value
    return result


def verified_derived(payload,attributes):
    fields=_fields(payload);accepted=[]
    for attr in attributes:
        text=fields.get(attr.get('source_field'));value=attr.get('value')
        start,end=attr.get('start'),attr.get('end')
        if (isinstance(text,str) and isinstance(value,str) and type(start) is int and type(end) is int
                and 0<=start<end<=len(text) and text[start:end]==value):
            # Classification and brand scope remain suggestions. Unsafe NFKC
            # aliases (e.g. M² -> M2) are deliberately not consumed here.
            brand=attr.get('brand_scope')
            if brand and (not isinstance(brand,str) or not any(brand in v for v in fields.values())):
                continue
            accepted.append(attr)
    return accepted


def build_clue_index(session,*,organization_id,store_id,target,derived_path=None):
    target=Path(target).resolve()
    if target.exists():raise ValueError('source_clue_new_target_required')
    refs,fingerprint=source_refs(session,organization_id,store_id)
    attributes={}
    if derived_path:
        from backend.app.modules.search.derived_catalog.store import read_current_terms
        attributes=read_current_terms(derived_path,organization_id=organization_id,store_id=store_id,sources=refs)
    expected={item['product_id']:item for item in refs}
    descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(descriptor)
    connection=sqlite3.connect(target)
    try:
        connection.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        connection.execute('CREATE TABLE clues (id INTEGER PRIMARY KEY,product_id TEXT NOT NULL,source_field TEXT NOT NULL,value TEXT NOT NULL,fold TEXT NOT NULL,kind TEXT NOT NULL,certainty TEXT NOT NULL,origin TEXT NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,comparison TEXT NOT NULL,model TEXT,extraction_version TEXT,brand_scope TEXT)')
        connection.execute('CREATE INDEX ix_clue_fold ON clues(fold,product_id)')
        connection.execute('CREATE INDEX ix_clue_product ON clues(product_id)')
        connection.execute('CREATE INDEX ix_clue_comparison ON clues(comparison,product_id)')
        statement=select(CatalogSnapshot.product_id,CatalogSnapshot.id,CatalogSnapshot.payload_sha256,CatalogSnapshot.payload).where(
            CatalogSnapshot.organization_id==organization_id,CatalogSnapshot.store_id==store_id).join(
                CatalogProduct,and_(CatalogProduct.current_snapshot_id==CatalogSnapshot.id,
                    CatalogProduct.id==CatalogSnapshot.product_id,
                    CatalogProduct.organization_id==CatalogSnapshot.organization_id,
                    CatalogProduct.store_id==CatalogSnapshot.store_id)).where(CatalogProduct.active.is_(True))
        observed,derived=0,0
        for identity,snapshot,digest,payload in session.execute(statement.execution_options(yield_per=100)):
            ref=expected.get(identity)
            if ref is None or (snapshot,digest)!=(ref['snapshot_id'],ref['payload_sha256']):continue
            fields=_fields(payload)
            for path,value in fields.items():
                connection.execute('INSERT INTO clues(product_id,source_field,value,fold,kind,certainty,origin,start,end,comparison,model,extraction_version,brand_scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (identity,path,value,value.casefold(),'source_field','observed_field','source',0,len(value),comparison_text(value),None,None,None))
                observed+=1
            for attr in verified_derived(payload,attributes.get(identity,[])):
                value=attr['value']
                connection.execute('INSERT INTO clues(product_id,source_field,value,fold,kind,certainty,origin,start,end,comparison,model,extraction_version,brand_scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (identity,attr['source_field'],value,value.casefold(),attr['kind'],attr['certainty'],attr.get('origin','derived'),attr['start'],attr['end'],comparison_text(value),attr.get('model'),attr.get('extraction_version'),attr.get('brand_scope')))
                derived+=1
        # The corpus is immutable after build; no GET triggers or repair writes.
        connection.execute("CREATE VIRTUAL TABLE clue_fts USING fts5(fold,comparison,content='clues',content_rowid='id',tokenize='trigram')")
        connection.execute("INSERT INTO clue_fts(clue_fts) VALUES ('rebuild')")
        meta=dict(schema_version=2,comparison_version="source-comparison-v2",organization_id=organization_id,store_id=store_id,source_fingerprint=fingerprint,
                  source_products=len(refs),source_clues=observed,derived_clues=derived,derived_products=len(attributes))
        connection.executemany('INSERT INTO metadata VALUES (?,?)',[(k,json.dumps(v)) for k,v in meta.items()])
        if source_refs(session,organization_id,store_id)[1]!=fingerprint:
            raise ValueError('source_changed_during_clue_build')
        connection.commit();target.chmod(0o600)
        return meta
    except Exception:connection.rollback();raise
    finally:connection.close()


def lookup(session,*,organization_id,store_id,query,index_path,exact_only=False,spec_terms=()):
    path=Path(index_path).resolve()
    if not path.is_file():return [],False,'unavailable'
    fingerprint=current_fingerprint(session,organization_id,store_id)
    connection=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        connection.execute('PRAGMA query_only=ON')
        meta={k:json.loads(v) for k,v in connection.execute('SELECT key,value FROM metadata')}
        if (meta.get('schema_version') not in (1,2) or meta.get('organization_id')!=organization_id or
                meta.get('store_id')!=store_id or meta.get('source_fingerprint')!=fingerprint):
            return [],False,'stale'
        needle=query.strip().casefold()
        if not needle:return [],False,'current'
        modern=meta.get('schema_version')==2
        comparison=comparison_text(query) if modern else needle
        use_comparison=modern and (comparison!=needle or any(' '<=c<='~' for c in needle))
        terms=list(dict.fromkeys((needle,comparison)))
        if exact_only:
            clause='c.fold=?';params=(needle,)
        else:
            clause='instr(c.fold,?)>0';params=(needle,)
            if use_comparison:
                clause='('+clause+' OR instr(c.comparison,?)>0)'
                params=(*params,comparison)
            if all(len(t)>=3 and '\x00' not in t for t in terms):
                phrase=' OR '.join('"'+term.replace('"','""')+'"' for term in terms)
                clause='c.id IN (SELECT rowid FROM clue_fts WHERE clue_fts MATCH ?) AND '+clause
                params=(phrase,*params)
        ids=[row[0] for row in connection.execute('SELECT DISTINCT c.product_id FROM clues c WHERE '+clause+' ORDER BY c.product_id LIMIT ?',(*params,MAX_RESULTS+1))]
        overflow=len(ids)>MAX_RESULTS;ids=ids[:MAX_RESULTS]
        matches=[]
        columns=('product_id','source_field','value','kind','certainty','origin','start','end')
        if modern:columns=(*columns,'model','extraction_version','brand_scope','comparison')
        selected=','.join('c.'+name for name in columns)
        for offset in range(0,len(ids),400):
            chunk=ids[offset:offset+400];marks=','.join('?' for _ in chunk)
            statement='SELECT '+selected+' FROM clues c WHERE c.product_id IN ('+marks+') AND '+clause
            statement+=" ORDER BY CASE c.origin WHEN 'model' THEN 0 WHEN 'rule' THEN 1 ELSE 2 END,c.id"
            for row in connection.execute(statement,(*chunk,*params)):
                matches.append(dict(zip(columns,row)))
        if spec_terms:
            spec_values={}
            for offset in range(0,len(ids),400):
                chunk=ids[offset:offset+400];marks=','.join('?' for _ in chunk)
                for pid,path,value in connection.execute(
                        "SELECT product_id,source_field,value FROM clues WHERE origin='source' AND product_id IN ("+marks+")",chunk):
                    if path.split('.')[-1] in ('code','name','spec','model'):
                        spec_values.setdefault(pid,[]).append(value)
            allowed={pid for pid,values in spec_values.items() if all(contains_term(comparison_text(term),
                        comparison_text('\n'.join(values))) for term in spec_terms)}
            matches=[row for row in matches if row['product_id'] in allowed]
        return matches,overflow,'current'
    except (sqlite3.Error,ValueError,KeyError,TypeError):return [],False,'unavailable'
    finally:connection.close()


def augment_search(session,*,organization_id,store_id,query,result,limit,index_path,spec_terms=()):
    from backend.app.modules.search import service
    try:
        clues,overflow,status=lookup(session,organization_id=organization_id,store_id=store_id,query=query,
                                     index_path=index_path,exact_only=False,spec_terms=spec_terms)
    except (OSError,ValueError,sqlite3.Error):clues,overflow,status=[],False,'unavailable'
    if status!='current':
        result['message']+=' 跨字段线索索引待更新或不可用，当前显示原始检索结果。'
        return result
    by_product={}
    for clue in clues:by_product.setdefault(clue['product_id'],[]).append(clue)
    existing={row['product_id']:row for row in [*result['exact'],*result['candidates']]}
    exact_ids={row['product_id'] for row in result['exact']}
    needle=query.strip().casefold();comp=comparison_text(query)
    ranks={pid:0 if row['match_type']=='normalized_barcode' else 2 for pid,row in existing.items()}
    # Every accepted attribute has its original whole field in the sidecar.
    # Check numeric boundaries on that whole field before applying the page cap;
    # a model's "6x20" subspan inside "16x20" must not consume a valid slot.
    safe={}
    for pid,items in by_product.items():
        fields={c['source_field']:c for c in items if c['origin']=='source'}
        paths={path for path,c in fields.items() if contains_term(needle,c['value'].casefold())
               or contains_term(comp,c['comparison'] if 'comparison' in c else comparison_text(c['value']))}
        accepted=[c for c in items if c['source_field'] in paths]
        if not accepted:continue
        safe[pid]=accepted
        strength=5
        for ev in accepted:
            name=ev['source_field'].split('.')[-1]
            equal=ev['value'].casefold()==needle or (ev['comparison'] if 'comparison' in ev else comparison_text(ev['value']))==comp
            if equal and name in ('code','barCode','model'):score=0
            elif equal and name not in ('pyCode','productCategoryIdName'):score=1
            elif name in ('name','shortName'):score=2
            elif name in ('spec','model','code','barCode'):score=3
            else:score=5
            strength=min(strength,score)
        ranks[pid]=min(ranks.get(pid,5),strength)
    # Rank light metadata first. Only hydrate the requested candidate page and
    # exact hits needing evidence; never load 1,000 full raw payloads for 20 rows.
    pool=set(safe).union(row['product_id'] for row in result['candidates'])-exact_ids
    positions={row['product_id']:i for i,row in enumerate(result['candidates'])}
    order=sorted(pool,key=lambda pid:(ranks.get(pid,2),0 if pid in existing else 1,
                                     positions.get(pid,0),pid))
    selected=order[:limit]
    identities=list(dict.fromkeys([*selected,*(pid for pid in exact_ids if pid in safe)]))
    verified={};changed=False
    for offset in range(0,len(identities),400):
        for product,snapshot in repository.products_with_snapshots(session,organization_id,store_id,400,identities[offset:offset+400]):
            fields=_fields(snapshot.payload)
            spec_text=comparison_text('\n'.join([product.external_code,*(value for path,value in fields.items()
                if path.split('.')[-1] in ('code','name','spec','model'))]))
            # Existing lexical candidates have already passed the plan's hard
            # spec check. New source-clue candidates must pass it here too.
            allowed=all(contains_term(comparison_text(term),spec_text) for term in spec_terms)
            valid=[]
            for clue in safe.get(product.id,[]):
                text=fields.get(clue['source_field']);start,end=clue['start'],clue['end']
                if (isinstance(text,str) and type(start) is int and type(end) is int
                    and 0<=start<end<=len(text) and text[start:end]==clue['value']
                    and (contains_term(needle,text.casefold()) or contains_term(comp,comparison_text(text)))):
                    valid.append({k:clue[k] for k in ('source_field','value','kind','certainty','origin',
                        'model','extraction_version','brand_scope') if clue.get(k) is not None})
            old=existing.get(product.id)
            if old:
                if old['source_version']!=product.source_version or old['captured_at']!=snapshot.captured_at:
                    changed=True;continue
                if valid:old['search_evidence']=valid[:8]
                if product.id not in exact_ids:verified[product.id]=old
            elif valid and allowed:
                row=service._match(product,snapshot,field='derived',match_type='fuzzy_candidate',original=query)
                row['search_evidence']=valid[:8];verified[product.id]=row
            elif product.id in selected:
                changed=True
    # lookup filtered hard specifications before pagination; source changes
    # during the current-snapshot recheck still require a partial-result flag.
    if any(pid not in verified for pid in selected):changed=True
    additions=any(pid not in existing for pid in verified)
    result['candidates']=[verified[pid] for pid in selected if pid in verified]
    result['candidate_total']=max(result['candidate_total'],len(pool))
    result['ambiguous']=result['ambiguous'] or len(pool|exact_ids)>1
    more=len(order)>limit or overflow or changed
    result['has_more']=result['has_more'] or more
    result['truncated']=result['truncated'] or overflow or changed
    if additions:
        result['message']+=' 已加入原字段/平行整理线索候选；旧编号、品牌内货号与规格仍需人工核实。'
    if more:
        result['message']=result['message'].replace('已返回全部候选','当前仅显示部分候选')
        result['message']+=' 仍有其他候选或来源变化，请缩小查询范围。'
    return result


def has_source_term(payload,query):
    original=query.strip().casefold();comp=comparison_text(query)
    return any(contains_term(original,value.casefold()) or contains_term(comp,comparison_text(value))
               for value in _fields(payload).values())
