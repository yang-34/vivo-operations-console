"""Adapt the audited TaskFlow vivo protocols to this project's account and journal store."""
import json,time
from store import db,decrypt,encrypt,now
from .taskflow.protocol_common import CURRENT,ActionRequired,ProtocolError
from .taskflow.sdk import UnknownWriteResult,CredentialsExpired

SOURCE_TASKS=(
 ('purchase_points','领取已到账购物积分'),('news_rewards','浏览器资讯阅读与领奖'),
 ('reservation_rewards','游戏中心实际预约与核验'),('customized_signs','当期站点业务签到'),
 ('app_tasks','官网 App 任务广场'),('main_sign_reminder','官网主签到提醒'),
 ('monopoly_tasks','大富翁任务与浏览'),('monopoly_rewards','大富翁已达标奖励'),
 ('koicarp_rewards','锦鲤已开奖奖励'),('golden_tasks','金蛋活动浏览任务'),
 ('quiz_rank_prizes','答题榜单奖品与地址资格'),('offline_orders','本人线下订单与核销条件'),
 ('appointment_catalog','新品活动完整目录'),('appointment_reserve','新品零元预约'),
 ('appointment_reminder','新品签到提醒'),
 ('gc_native_activity_catalog','游戏中心原生活动目录'),('gc_native_week_sign','游戏中心福利周签到'),
 ('gc_native_vip_tickets','游戏中心免费 VIP 券'),('gc_native_download_rewards','游戏中心已达标下载福利'),
 ('wallet_native_assets','钱包独立账号资产'),('wallet_native_sign','钱包省钱专区签到'),
)

CORE_KEYS={
 'profile':'balance.sync','sign_in':'sign.main','home_welfare':'mall.claim','pending_points':'mall.claim','mall_events':'mall.claim',
 'member_benefits':'member.claim','member_applet_benefits':'member.applet','member_growth':'member.xp',
 'quiz':'quiz.answer','quiz_rewards':'quiz.gift','weekly_tasks':'tasks.weekly','weekly_codes':'tasks.weekly',
 'golden_free':'activity.egg','double11_free':'activity.double11','shop_favorite':'member.favorite','community_reads':'tasks.read',
 'game_instant':'activity.game','game_download':'activity.game','game_reserve':'activity.game',
 'game_instant_execute':'activity.game','game_download_execute':'activity.game','game_reserve_execute':'activity.game',
 'sign_notification':'sign.notify','guess_rewards':'mall.receive','guess_subscription':'mall.subscribe',
 'powerball_subscription':'lottery.status','coupon_reminder':'activity.remind','double11_remind':'activity.remind',
 'crowdfunding_codes':'quiz.lucky','lottery_activity_free':'activity.lottery','wish_favorite':'member.wish',
 'store_pickup_codes':'tasks.store','current_sites':'activity.dynamic','site_free_draws':'activity.dynamic',
 'consecutive_rewards':'sign.consecutive','phone_rewards':'tasks.phone','appointment_sign':'sign.appointment',
 'appointment_browse':'activity.tasks','appointment_draw':'activity.appointment','appointment_comments':'activity.comment',
}

def core_key(key):
    if key in CORE_KEYS:return CORE_KEYS[key]
    if key.startswith('sign_'):return 'sign.biz'
    if key.startswith('client_tasks_'):return None
    if key.startswith('tasks_'):return 'tasks.center'
    if key.startswith('ec_'):return 'tasks.ec'
    if key.startswith('sdk_'):return 'source.'+key
    return None

from .taskflow.vivo import LiveVivo as _SourceAdapter
from .task_policy import task_reason,subtask_reason,prune_result
SOURCE_TASKS=tuple(dict(SOURCE_TASKS+tuple((spec.key,spec.label) for spec in _SourceAdapter.tasks if spec.automatic and not core_key(spec.key))).items())


class ActiveSourceAdapter(_SourceAdapter):
    def task_group(self,client,context,key,snapshot,*,load=None,refresh_profile=True):
        group=key.split('_',1)[1]
        read=load or ((lambda:client.ec_list(group)) if key.startswith('ec_') else lambda:client.group_list(group))
        return super().task_group(client,context,key,snapshot,load=lambda:[t for t in read() if not subtask_reason(t)],refresh_profile=refresh_profile)

def task_catalog():
    tasks=[{'key':spec.key,'name':spec.label,'handler':core_key(spec.key) or 'source.'+spec.key,'default_enabled':spec.automatic,'requirements':spec.reason or ('需要绑定本人 Android 原生设备及对应 App 会话' if spec.key.startswith(('browser_','gc_native_','wallet_native_')) else '以平台资格与实际进度为准'),'source':'TaskFlow '+_SourceAdapter.version} for spec in _SourceAdapter.tasks]
    for task in tasks:
        if task['key']=='quiz':
            task.update(default_enabled=True, requirements='仅平台明确本轮入场费用为 0 时执行；费用未知或需要积分时跳过')
    tasks.extend({'key':key,'name':name,'handler':'manual-confirmation','default_enabled':False,'requirements':'消耗积分，禁止加入自动批次','source':'原项目活动协议'} for key,name in [('paid_quiz','付费答题'),('paid_draw','购买抽奖机会'),('charity','积分公益捐赠'),('bean_exchange','资产转换'),('guess_bet','积分竞猜'),('koi_bet','锦鲤积分参与'),('powerball_bet','积分彩票'),('treasure_bet','积分夺宝')])
    tasks.extend({'key':key,'name':name,'handler':key,'default_enabled':True,'requirements':'以当前会场资格与免费次数为准','source':'用户提供 HAR'} for key,name in [('har.activities','HAR 新增会场及免费任务'),('har.membership','商城新人礼包及会场免费券')])
    for task in tasks:
        reason=task_reason(task['key'],task['handler'])
        if reason:task.update(default_enabled=False,temporarily_removed=True,requirements=reason)
    return tasks


def run_groups(api, keys):
    """Keep each package's journal and isolate unavailable groups."""
    active=[key for key in keys if not task_reason(key,core_key(key) or 'source.'+key)]
    result={'results': [dict(run_source(api, key), task_key=key) for key in active]}
    if len(active)!=len(keys):result['excluded_count']=len(keys)-len(active)
    return result


class Context:
    def __init__(self,api,mode,account_id,raw):
        self.mode=mode;self.account_id=account_id;self.transport=api.s;self.checkpoint={}
        self.credentials=dict(raw);session={**raw,**(raw.get('session') or {})}
        from .devices import resolve_device,webview_ua
        device=dict(getattr(api.acct,'device',None) or resolve_device(raw))
        device.update(imei=api.acct.imei,oaid=api.acct.oaid)
        self.credentials.update(session=session,device=device,user_agent=webview_ua(device),external_id=api.acct.openid)
        with db(mode) as c:row=c.execute('SELECT payload FROM account_scopes WHERE account_id=?',(account_id,)).fetchone() if account_id else None
        self.credentials['scopes']={**(raw.get('scopes') or {}),**(decrypt(row[0]) if row else {})}
    def ensure_allowed(self):
        if self.transport.guard:self.transport.guard()
    def persist_scopes(self,scopes):
        self.credentials['scopes']=scopes
        if self.account_id:
            with db(self.mode) as c:c.execute('INSERT OR REPLACE INTO account_scopes VALUES(?,?,?)',(self.account_id,encrypt(scopes),now()))


def run_source(api,key):
    from .taskflow.vivo import VivoClient,LiveVivo
    from .taskflow.added_common import report
    reason=task_reason(key,core_key(key) or 'source.'+key)
    if reason:return {'status':'skipped','skip':'任务已暂时移除','removed':True,'reason':reason}
    context=api.source_context
    if key in ('reservation_rewards','gc_native_activity_catalog','gc_native_week_sign','gc_native_vip_tickets','gc_native_download_rewards','wallet_native_assets','wallet_native_sign') and not context.credentials['device'].get('native_verified'):
        return {'status':'partial','results':[{'name':dict(SOURCE_TASKS).get(key,key),'status':'blocked','reason':'当前使用 vivo 协议设备配置；原生任务还需要本账号原始 vivo 设备会话，不能用通用配置代替'}]}
    token=CURRENT.set(context)
    try:
        if not hasattr(api,'source_client'):api.source_client=VivoClient(context.credentials,context.ensure_allowed)
        client=api.source_client;client.http.writing=False
        from .taskflow.vendor.vivo.taskcenter import DEFAULT_PACKAGE
        client.pkg=DEFAULT_PACKAGE
        adapter=ActiveSourceAdapter();snapshot={'points':api.pm_points()};balance_before=snapshot['points'];entries=[];previous=None
        while True:
            client.pkg=DEFAULT_PACKAGE
            api.partial_result={'source':key,'results':entries}
            result=adapter.run(client,context,key,snapshot)
            entries.extend(result.diagnostics.get('entries') or [{'name':dict(SOURCE_TASKS).get(key,key),'status':result.status,'message':result.message,**result.diagnostics}])
            snapshot=result.snapshot
            if result.continue_after is None:break
            marker=json.dumps([snapshot,context.credentials.get('scopes')],sort_keys=True,default=str)
            if marker==previous:return {'status':'partial','reason':'继续条件未变化，保留断点等待下一次核验','results':entries}
            previous=marker;time.sleep(min(float(result.continue_after),2))
        statuses={'success':'completed','action_required':'blocked','skipped':'skipped','failed':'failed'}
        for entry in entries:
            entry['status']=statuses.get(entry.get('status'),entry.get('status','pending'))
            entry['reason']=entry.pop('message',entry.get('reason',''))
        state=statuses.get(result.status,'partial')
        if state=='blocked':state='partial'
        return prune_result({'status':state,'reason':result.message,'results':entries,'points_before':balance_before,'points_after':snapshot.get('points',balance_before)})
    except (ActionRequired,CredentialsExpired) as error:
        failed=getattr(error,'diagnostics',{}).get('provider_result_status')=='failed'
        return {'status':'failed' if failed else 'partial','results':[{'name':dict(SOURCE_TASKS).get(key,key),'status':'failed' if failed else 'blocked','reason':getattr(error,'message',str(error)),'details':getattr(error,'diagnostics',{})}]}
    except UnknownWriteResult:
        return {'status':'partial','reason':'前次提交结果未确定，已保存日志并禁止重复提交'}
    except ProtocolError as error:return {'status':'failed','reason':getattr(error,'message',str(error))}
    finally:CURRENT.reset(token)
