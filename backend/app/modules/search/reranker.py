"""Optional local cross-encoder; scores rank observed candidates, never create facts."""
from hashlib import sha256
import json
from pathlib import Path
import threading

from backend.app.modules.search.embedding import EmbeddingUnavailable


class LocalCrossEncoder:
    def __init__(self,directory):
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
            folder=Path(directory).resolve();manifest=json.loads((folder/'manifest.json').read_text())
            if manifest.get('algorithm')!='bge-rerank-logit-v1':raise ValueError
            for name in ('model_quantized.onnx','tokenizer.json'):
                if sha256((folder/name).read_bytes()).hexdigest()!=manifest['sha256'][name]:raise ValueError
            self.identity=sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            self.np=np;self.tokenizer=Tokenizer.from_file(str(folder/'tokenizer.json'))
            self.tokenizer.enable_truncation(max_length=256)
            self.tokenizer.enable_padding(pad_id=1,pad_token='<pad>')
            options=ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1
            self.runtime=ort.InferenceSession(str(folder/'model_quantized.onnx'),sess_options=options,
                                             providers=['CPUExecutionProvider'])
            self.lock=threading.Lock()
        except Exception:raise EmbeddingUnavailable('reranker_unavailable') from None

    def score(self,query,documents):
        if not documents:return []
        try:
            scores=[]
            with self.lock:
                for offset in range(0,len(documents),8):
                    batch=self.tokenizer.encode_batch([(query,doc) for doc in documents[offset:offset+8]])
                    arrays={'input_ids':self.np.asarray([v.ids for v in batch],dtype='int64'),
                            'attention_mask':self.np.asarray([v.attention_mask for v in batch],dtype='int64'),
                            'token_type_ids':self.np.asarray([v.type_ids for v in batch],dtype='int64')}
                    output=self.runtime.run(None,{v.name:arrays[v.name] for v in self.runtime.get_inputs()})[0]
                    values=self.np.asarray(output).reshape(-1)
                    if len(values)!=len(batch) or not self.np.isfinite(values).all():raise ValueError
                    scores.extend(float(value) for value in values)
            return scores
        except Exception:raise EmbeddingUnavailable('reranker_inference_unavailable') from None
