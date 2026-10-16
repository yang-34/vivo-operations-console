export type Row = Record<string, any>
export type Mode = 'demo' | 'live'
let csrfToken=''
export function requestId(){return globalThis.crypto?.randomUUID?.()||`${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`}
async function csrf(){if(!csrfToken){const r=await fetch('/api/auth/session');if(r.status===401){location.assign('/login');throw new Error('请先登录')}csrfToken=(await r.json()).csrf_token}return csrfToken}
export async function api<T = any>(path: string, mode: Mode, options: RequestInit = {}): Promise<T> {
  const token=options.method&&options.method!=='GET'?await csrf():''
  const res = await fetch(`/api${path}`, {...options, headers:{'Content-Type':'application/json','X-Data-Mode':mode,...(token?{'X-CSRF-Token':token}:{}),...options.headers}})
  if(res.status===401){location.assign('/login');throw new Error('会话已过期，请重新登录')}
  const data = await res.json()
  if (!res.ok) throw new Error(data.error || `请求失败 (${res.status})`)
  return data
}
export function download(path: string, mode: Mode) {
  fetch(`/api${path}`,{headers:{'X-Data-Mode':mode}}).then(async res=>{if(!res.ok)throw new Error('导出失败'); const blob=await res.blob(); const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`vivo-${path.includes('batch')?'batch':'accounts'}-${new Date().toISOString().slice(0,10)}.csv`;a.click();URL.revokeObjectURL(a.href)}).catch(()=>window.alert('导出失败，请检查本地服务连接后重试。'))
}
