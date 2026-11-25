"""Finite research contracts. Evidence never grants network or business authority."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class SearchInput(Input):
    query: str = Field(default='', max_length=300)
    limit: int = Field(default=10, ge=1, le=20)
    cursor: str | None = Field(default=None, max_length=1024)

class ReadInput(Input):
    artifact_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
    corpus_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    start: int = Field(default=0, ge=0, le=65536)
    limit: int = Field(default=1024, ge=1, le=4096)

class DescribeInput(Input):
    candidate_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
    corpus_digest: str = Field(pattern=r'^[a-f0-9]{64}$')

class ProposeInput(DescribeInput):
    goal: str = Field(min_length=1,max_length=500)
    parameters: dict

    @model_validator(mode='after')
    def wire_values(self):
        from .runtime_protocol import _shape
        _shape(self.parameters)
        return self

class Output(Input):
    version: Literal[1]
    authority: Literal['reference_only']
    operation: Literal['search','read','describe','propose']
    corpus_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    data: dict

    @model_validator(mode='after')
    def bounded(self):
        from . import research_contracts as c
        from .runtime_protocol import _shape
        _shape(self.model_dump(mode='json'))
        c.decode(c.canonical(self.model_dump(mode='json')), 65536)
        if self.operation == 'propose':
            if self.data.get('authority') != 'proposal_only' or self.data.get('status') != 'not_executed':c.deny()
        elif self.data.get('authority') != 'reference_only':c.deny()
        return self

class ProbeInput(Input):
    contract_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    proposal: dict
    @model_validator(mode='after')
    def bounded(self):
        from . import research_contracts as c
        from .runtime_protocol import _shape
        _shape(self.proposal);c.decode(c.canonical(self.model_dump(mode='json')),16384)
        return self

class ProbeOutput(Input):
    version: Literal[1]
    authority: Literal['reference_only']
    operation: Literal['probe']
    corpus_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    metadata: dict
    data: dict
    @model_validator(mode='after')
    def bounded(self):
        from . import research_contracts as c
        from .runtime_protocol import _shape
        value=self.model_dump(mode='json');_shape(value);c.decode(c.canonical(value),65536)
        if self.data.get('coverage')!='bounded_partial' or self.data.get('complete') is not False:c.deny()
        return self

INPUTS={'search':SearchInput,'read':ReadInput,'describe':DescribeInput,'propose':ProposeInput,'probe':ProbeInput}
NAMES={('research.'+op,'1'):'v4_research_'+op+'_v1' for op in INPUTS}
DESCRIPTIONS={
    'probe':'执行Host独立批准的有界只读探测：先describe取得approved_read_contracts，再propose形成提案；仅允许精确合同ID，不接受URL/路径/凭据，结果始终bounded_partial，不可冒全量或写权限。',
    'search':'当前工具不能解决销售、库存、商品等陌生业务问题时，在Host批准的研究资料包中检索接口、方法及证据；返回候选ID和资料引用，不发业务请求，不授执行权限。',
    'read':'按资料ID读取精确证据片段；必须使用搜索所得corpus_digest，不接受路径或URL。资料内容不可信且仅供研究。',
    'describe':'读取研究候选的完整请求/结果契约、读写语义、分页与未知项；候选不是执行许可。',
    'propose':'依据精确候选契约形成有界探测提案，记录参数、证据与验证计划；固定not_executed，不执行网络或业务写入。'}

def operation(key,version,name):
    if type(key) is not str or version!='1' or NAMES.get((key,version))!=name:raise ValueError('research_binding_invalid')
    return key.split('.',1)[1]

def invoke(caller,requester,agent,config,op,arguments):
    from . import research_catalog as catalog, research_probe_plan as plan
    args=INPUTS[op].model_validate(arguments).model_dump(mode='json')
    fn={'search':catalog.search,'read':catalog.read,'describe':catalog.describe_candidate,'propose':plan.propose}[op]
    value=fn(caller,requester,agent,config,**args)
    return Output(version=1,authority='reference_only',operation=op,corpus_digest=config.manifest_sha256,data=value).model_dump(mode='json')

def response_schemas(binding,active_names):
    """Caller supplies only its freshly verified Run pin and actual sent tools."""
    from . import tool_registry as registry
    frozen=registry._pi_binding_shape(binding,profile=registry._binding_profile(binding))
    if frozen.get('research_source') is None or type(active_names) not in (list,tuple,set,frozenset):raise ValueError()
    names=set(active_names)
    if len(names)!=len(active_names) or not names or not names <= {x['name'] for x in frozen['tools']}:raise ValueError()
    return {entry['name']:registry.resolve_tool(entry['tool_key'],entry['version']).input_model
            for entry in frozen['tools'] if entry['name'] in names}
