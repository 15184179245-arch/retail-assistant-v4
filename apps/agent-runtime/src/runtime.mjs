// Public SDK owns the only inference/session loop. Provider is trusted launch input,
// never a serialized module path or credential selected by the wire peer.
import {createHash} from 'node:crypto';
import * as sdk from '@earendil-works/pi-coding-agent';
import * as ai from '@earendil-works/pi-ai';
export async function runReadonlySession({start,channel,model,produce,resolveAuth,policy,sessionDir,observe=()=>{}}){
 let session;
 const stream=(actual,context,options={})=>{
  const out=ai.createAssistantMessageEventStream();
  queueMicrotask(async()=>{
   let message;
   try{
    if(options.signal?.aborted)throw new Error('aborted');
    const tools=ai.getCurrentTools(context.messages);
    if(tools.length!==1||tools[0].name!=='tasks.list_mine@1')throw new Error('tool_profile');
    const input_digest=createHash('sha256').update(JSON.stringify(context.messages)).digest('hex');
    observe('model_attempt',{});
    const permit=await channel.request('model_begin',{input_digest});
    if(options.signal?.aborted)throw new Error('aborted');
    observe('host_permit',{permit:permit.permit,step_id:permit.step_id});
    // Only the trusted injected provider performs a physical call, after permit.
    message=await produce(actual,context,options,permit);
    observe('physical_result',{step_id:permit.step_id});
    if(options.signal?.aborted)throw new Error('aborted');
    await channel.request('model_complete',{permit:permit.permit,step_id:permit.step_id,message:{role:message.role,content:message.content,usage:{input:message.usage.input,output:message.usage.output},stopReason:message.stopReason}});
    if(options.signal?.aborted)throw new Error('aborted');
    out.push({type:'start',partial:{...message,content:[]}});out.push({type:'done',reason:message.stopReason,message});out.end(message);
   }catch(error){
    channel.fail(error);
    const m={role:'assistant',content:[],api:model.api,provider:model.provider,model:model.id,usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}},stopReason:'error',errorMessage:'host_runtime_failed',timestamp:Date.now()};
    out.push({type:'error',reason:'error',error:m});out.end(m);
   }
  });return out;
 };
 const prepareArguments=raw=>{
  if(raw===null||typeof raw!=='object'||Array.isArray(raw)||Object.keys(raw).some(k=>k!=='limit')||('limit'in raw&&(!Number.isSafeInteger(raw.limit)||raw.limit<1||raw.limit>20)))throw new Error('raw_tool_arguments');return raw;
 };
 const execute=async(id,args,signal)=>{if(signal?.aborted)throw new Error('aborted');const result=await channel.request('tool_request',{call_id:id,arguments:args});observe('tool_result',result);return {content:[{type:'text',text:JSON.stringify(result)}],details:{source:'host-run',callId:id}};};
 try{
  const modelRuntime=await sdk.ModelRuntime.create({credentials:new ai.InMemoryCredentialStore(),modelsPath:null,refreshOnCreate:false,allowModelNetwork:false});
  modelRuntime.registerNativeProvider({id:model.provider,name:'Host controlled',auth:{apiKey:{name:'host-controlled',resolve:resolveAuth}},getModels:()=>[model],stream,streamSimple:stream});
  const settings=sdk.SettingsManager.inMemory({cacheWarming:'off',compaction:{enabled:false,reserveTokens:128,keepRecentTokens:16},retry:{enabled:false,maxRetries:0,provider:{maxRetries:0}},codemode:{mode:'inline'}},{projectTrusted:false});
  const loader=new sdk.DefaultResourceLoader({...policy,settingsManager:settings,systemPrompt:'Use only the Host-authorized read tool.',appendSystemPrompt:[],extensionFactories:[]});await loader.reload();
  if(settings.isProjectTrusted()||loader.getExtensions().extensions.length||loader.getSkills().skills.length||loader.getPrompts().prompts.length||loader.getThemes().themes.length||loader.getAgentsFiles().agentsFiles.length)throw new Error('resource_profile');
  const manager=sdk.SessionManager.create(policy.cwd,sessionDir);
  const tool={name:start.tool_name,label:'Host task read',description:'Read tasks belonging to the current authorized requester.',parameters:start.input_schema,executionMode:'sequential',prepareArguments,execute};
  ({session}=await sdk.createAgentSession({cwd:policy.cwd,agentDir:policy.agentDir,modelRuntime,model:modelRuntime.getModel(model.provider,model.id),thinkingLevel:'off',settingsManager:settings,sessionManager:manager,resourceLoader:loader,customTools:[tool],noTools:'all',tools:[start.tool_name]}));
  session.agent.toolExecution='sequential';await session.bindExtensions({mode:'print',onError:()=>{throw new Error('extension_error');}});
  if(JSON.stringify(session.getActiveToolNames())!==JSON.stringify([start.tool_name]))throw new Error('tool_profile');
  session.subscribe(e=>observe('sdk_event',e));await session.prompt(start.prompt);await session.waitForIdle();
  if(channel.error)throw channel.error;observe('sdk_messages',manager.buildSessionProjection().messages);
 }finally{session?.dispose();}
}
