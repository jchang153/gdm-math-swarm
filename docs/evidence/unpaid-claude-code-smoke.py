import asyncio,json
from pathlib import Path
from inspect_ai import Task,eval_async
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelOutput
from research_swarm.inspect_adapter import build_claude_agent,conference_tools
from research_swarm.runtime import write_compose
calls=[]
class Service:
 def action(self,agent_id,name,args):
  calls.append({'agent_id':agent_id,'name':name,'args':args})
  return {'value':'private-peer-result'}
request_count=0
async def filter(model,messages,tools,tool_choice,config):
 global request_count
 request_count+=1
 if request_count>5:raise RuntimeError('Unexpected smoke loop')
 if request_count==1:
  return ModelOutput.for_tool_call(model='mockllm/model',tool_name='Bash',tool_arguments={'command':'python3 -c \"from pathlib import Path; Path(\'/workspace/RESEARCH.md\').write_text(\'scripted notebook\\n\'); print(\'local tool result\')\"'},tool_call_id='smoke_local_1')
 if request_count==2:
  name=next(t.name for t in tools if 'probe' in t.name)
  return ModelOutput.for_tool_call(model='mockllm/model',tool_name=name,tool_arguments={'message':'scripted-check'},tool_call_id='smoke_call_1')
 return ModelOutput.from_content(model='mockllm/model',content='Scripted integration check complete.')
definitions=[{'name':'probe','description':'Probe the conference integration.','inputSchema':{'type':'object','properties':{'message':{'type':'string'}},'required':['message']}}]
compose=write_compose('/tmp/gdm-cli-smoke/compose.yaml','gdm-claude-code-smoke:local')
agent=build_claude_agent('Perform the scripted conference integration check.',conference_tools(Service(),'agent-001',definitions),filter)
task=Task(name='unpaid_cli_bridge_smoke',dataset=[Sample(input='Probe the conference tool, then finish.')],solver=agent,sandbox=('docker',compose))
async def main():
 logs=await eval_async(task,model='mockllm/model',score=False,log_dir='/tmp/gdm-cli-smoke/logs',log_model_api=True,sandbox_prebuilt=True,time_limit=120,retry_on_error=0,fail_on_error=False)
 summary={'status':logs[0].status,'scripted_model_requests':request_count,'conference_calls':calls,'paid_calls':0,'sample_errors':[str(sample.error) for sample in logs[0].samples if sample.error]}
 Path('/tmp/gdm-cli-smoke/summary.json').write_text(json.dumps(summary,indent=2))
 print(json.dumps(summary))
asyncio.run(main())
