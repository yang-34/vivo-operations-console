let active=false;
addEventListener('message',event=>{
  const data=event.data;
  if(active||event.source!==parent||data?.type!=='vivo-init'||!/^[a-f0-9]{32}$/.test(data.nonce||''))return;
  try{
    const url=new URL(data.sdk.url);
    if(url.protocol!=='https:'||url.username||url.password||!/(^|\.)(vivo\.com\.cn|dingxiang-inc\.com)$/.test(url.hostname))return;
    active=true;
    const send=extra=>parent.postMessage({type:'vivo-result',nonce:data.nonce,...extra},event.origin);
    const script=document.createElement('script');script.src=url.href;
    script.onload=()=>{try{const widget=new VivoCaptcha(data.sdk.params,{element:'vivo-captcha',language:'cn',style:'popup',width:280,verifySuccess:ticket=>{send({ticket,constId:widget.constId()});widget.hide();},loadSdkFail:()=>send({error:'SDK_LOAD'})});widget.show();}catch{send({error:'SDK_INIT'})}};
    script.onerror=()=>send({error:'SDK_LOAD'});document.head.appendChild(script);
  }catch{}
});
