"""Source exact wins; fuse lexical and semantic candidates with source rechecks."""
from functools import lru_cache
import os
from pathlib import Path

from backend.app.modules.catalog import repository
from backend.app.modules.search import service
from backend.app.modules.search.embedding import LocalChineseEmbedding,EmbeddingUnavailable
from backend.app.modules.search.refinement import refine_products
from backend.app.modules.search.vector_index import vector_search,VectorUnavailable
from backend.app.modules.search.reranker import LocalCrossEncoder


@lru_cache(maxsize=2)
def _embedding(path, version):
    return LocalChineseEmbedding(path)


@lru_cache(maxsize=2)
def _reranker(path,version):
    return LocalCrossEncoder(path)


def search_hybrid(session,*,organization_id,store_id,query,plan,limit=10,embedder=None,index_path=None,reranker=None):
    lexical=refine_products(session,organization_id=organization_id,store_id=store_id,query=query,plan=plan,limit=20)
    if lexical['exact']:return lexical
    try:
        index_path=index_path or os.environ.get('SEARCH_VECTOR_INDEX_PATH','')
        model_path=os.environ.get('SEARCH_EMBEDDING_MODEL_PATH','')
        if not index_path or (embedder is None and not model_path):raise VectorUnavailable('vector_not_configured')
        embedder=embedder or _embedding(model_path,Path(model_path,'manifest.json').stat().st_mtime_ns)
        semantic_query=' '.join([*(term for group in plan.keyword_groups for term in group),*plan.spec_terms])
        semantic=vector_search(session,organization_id=organization_id,store_id=store_id,
            query=semantic_query,spec_terms=plan.spec_terms,embedder=embedder,index_path=index_path,limit=40)
    except (VectorUnavailable,EmbeddingUnavailable,ImportError,OSError):
        lexical['candidates']=lexical['candidates'][:limit]
        lexical['has_more']=lexical['has_more'] or lexical['candidate_total']>limit
        lexical['message']+=' 向量索引未就绪或不可用，已使用确定性条件检索。'
        return lexical
    scores={};snapshots={};lexical_by_id={m['product_id']:m for m in lexical['candidates']}
    for channel in ([m['product_id'] for m in lexical['candidates']],[m['product_id'] for m in semantic]):
        for rank,identity in enumerate(channel,1):scores[identity]=scores.get(identity,0)+1/(60+rank)
    for item in semantic:snapshots[item['product_id']]=item['snapshot_id']
    order=sorted(scores,key=lambda identity:(-scores[identity],identity))
    fetched={p.id:(p,s) for p,s in repository.products_with_snapshots(session,organization_id,store_id,len(order),order)}
    matches=[];changed=False
    for identity in order:
        pair=fetched.get(identity)
        if pair is None:changed=True;continue
        product,snapshot=pair
        old=lexical_by_id.get(identity)
        # Both channels must still refer to exactly the snapshot they checked.
        if ((identity in snapshots and snapshot.id!=snapshots[identity]) or
                (old and (old['source_version']!=product.source_version or old['captured_at']!=snapshot.captured_at))):
            changed=True;continue
        match=service._match(product,snapshot,field=old['match_field'] if old else 'name',
            match_type='fuzzy_candidate' if old else 'semantic_candidate',original=query)
        if old and old.get('search_evidence'):match['search_evidence']=old['search_evidence']
        matches.append(match)
    total=len(matches)
    reranked=False;rerank_failed=False
    reranker_path=os.environ.get('SEARCH_RERANKER_MODEL_PATH','')
    if matches and (reranker is not None or reranker_path):
        try:
            reranker=reranker or _reranker(reranker_path,Path(reranker_path,'manifest.json').stat().st_mtime_ns)
            documents=['；'.join(str(row.get(key) or '') for key in ('name','spec','unit')) for row in matches]
            values=reranker.score(query,documents)
            import math
            if len(values)!=len(matches) or any(not math.isfinite(v) for v in values):
                raise EmbeddingUnavailable('reranker_output_invalid')
            matches=[row for _,row in sorted(zip(values,matches),key=lambda item:-item[0])]
            reranked=True
        except (EmbeddingUnavailable,OSError,ImportError):
            rerank_failed=True
    more=total>limit or lexical['has_more'] or len(semantic)==40 or changed
    return dict(query=query,exact=[],candidates=matches[:limit],ambiguous=total>1 or lexical['ambiguous'],
        exact_total=0,candidate_total=total,has_more=more,
        scan_truncated=lexical['scan_truncated'],truncated=lexical['truncated'] or changed,
        message='关键词与中文向量候选已融合；相似度不代表身份或库存已确认，需员工核对规格。'
        +' 当前数量为有界候选池，不是全部相关商品总量。'
        +(' 候选已按描述相关程度重新排序，仍需人工确认。' if reranked else '')
        +(' 相关程度排序暂不可用，保留搜索结果。' if rerank_failed else '')
        +(' 仅展示部分候选，请补充规格。' if more else '')
        +(' 来源变化或扫描受限，请重新查询。' if changed or lexical['truncated'] else ''))
