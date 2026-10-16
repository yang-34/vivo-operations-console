from dataclasses import dataclass,field

class PlatformError(Exception):
    code='platform_error'
    def __init__(self,message='平台操作未完成',**diagnostics):super().__init__(message);self.message=message;self.diagnostics=diagnostics
class CredentialsExpired(PlatformError):pass
class UnknownWriteResult(PlatformError):
    def __init__(self,message='提交结果不确定，仅回查状态，不重复提交'):super().__init__(message,reason_kind='settlement')
@dataclass
class TaskSpec:
    key:str
    label:str
    writes:bool=False
    automatic:bool=True
    reason:str=''
@dataclass
class StepResult:
    status:str
    message:str
    snapshot:dict
    credential_updates:dict=field(default_factory=dict)
    continue_after:float|None=None
    pending_write:bool=False
    diagnostics:dict=field(default_factory=dict)
    followup_after:float|None=None
@dataclass
class Identity:
    external_id:str
    label:str
    snapshot:dict
