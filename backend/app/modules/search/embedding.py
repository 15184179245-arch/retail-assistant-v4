"""Optional, local ONNX Chinese embeddings. No downloads or remote code at runtime."""
from hashlib import sha256
import json
from pathlib import Path
import threading


class EmbeddingUnavailable(ValueError):
    pass


class LocalChineseEmbedding:
    def __init__(self,directory):
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
            folder=Path(directory).resolve()
            manifest=json.loads((folder/'manifest.json').read_text())
            if manifest.get('algorithm')!='bge-cls-l2-v1': raise ValueError
            for name in ('model_quantized.onnx','tokenizer.json'):
                if sha256((folder/name).read_bytes()).hexdigest()!=manifest['sha256'][name]: raise ValueError
            self.identity=sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            self.np=np
            self.tokenizer=Tokenizer.from_file(str(folder/'tokenizer.json'))
            self.tokenizer.enable_truncation(max_length=256)
            self.tokenizer.enable_padding(pad_id=0,pad_token='[PAD]')
            options=ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1
            self.runtime=ort.InferenceSession(str(folder/'model_quantized.onnx'),sess_options=options,
                                             providers=['CPUExecutionProvider'])
            self.lock=threading.Lock()
        except Exception:
            raise EmbeddingUnavailable('embedding_model_unavailable') from None

    def encode(self,texts,*,query=False):
        try:
            return self._encode(texts,query=query)
        except EmbeddingUnavailable:
            raise
        except Exception:
            raise EmbeddingUnavailable('embedding_inference_unavailable') from None

    def _encode(self,texts,*,query=False):
        if not texts: raise ValueError('embedding_empty_batch')
        if query: texts=['为这个句子生成表示以用于检索相关文章：'+text for text in texts]
        with self.lock:
            encoded=self.tokenizer.encode_batch(texts)
            arrays={'input_ids':self.np.asarray([v.ids for v in encoded],dtype='int64'),
                    'attention_mask':self.np.asarray([v.attention_mask for v in encoded],dtype='int64'),
                    'token_type_ids':self.np.asarray([v.type_ids for v in encoded],dtype='int64')}
            outputs=self.runtime.run(None,{v.name:arrays[v.name] for v in self.runtime.get_inputs()})
        vectors=self.np.asarray(outputs[0],dtype='float32')
        if vectors.ndim==3: vectors=vectors[:,0,:]
        if vectors.ndim!=2 or len(vectors)!=len(texts) or not self.np.isfinite(vectors).all():
            raise EmbeddingUnavailable('embedding_output_invalid')
        norms=self.np.linalg.norm(vectors,axis=1,keepdims=True)
        if self.np.any(norms<=0):raise EmbeddingUnavailable('embedding_output_invalid')
        return vectors/norms
