import json
from types import SimpleNamespace
from unittest.mock import Mock

from store import db,encrypt,now


def activate(config):
    import store
    (store.DATA/'task-exclusions.json').write_text(json.dumps(config,ensure_ascii=False),encoding='utf-8')


def test_disabled_steps_are_removed_from_plan_and_catalog_but_kept_for_review(client):
    from protocol.runner import step_plan
    from protocol.api import VivoApi,Account
    activate({'steps':{'source.browser_video':'没有设备','quiz.answer':'当前需要积分'}})
    api=VivoApi(Account({'openid':'owned','vivotoken':'token'}))
    try:keys={key for key,_,_ in step_plan(api,'full')}
    finally:api.s.close()
    assert 'source.browser_video' not in keys and 'quiz.answer' not in keys and 'sign.main' in keys
    catalog=client.get('/api/tasks/catalog').json
    assert 'browser_video' not in {r['key'] for r in catalog['items']}
    removed={r['key']:r for r in catalog['removed']}
    assert removed['browser_video']['requirements']=='没有设备'
    assert removed['browser_video']['temporarily_removed']


def test_manual_and_native_subtasks_are_pruned_before_execution_but_ready_rewards_remain(client):
    from protocol.task_policy import filter_response
    activate({'remove_manual_subtasks':True,'remove_native_subtasks':True})
    value={'code':0,'data':{'tasks':[{'type':0,'hasMore':True,'list':[
        {'id':1,'name':'钱包贷款活动任务'}, {'id':2,'name':'观看5分钟视频'},
        {'id':3,'name':'浏览会员页面','webTask':True},
        {'id':4,'name':'门店购机送积分','unreceviedPoints':5},
        {'id':5,'name':'贷款','taskComplete':False,'completedTimes':1,'totalTime':1},
    ]}]}}
    filtered=filter_response('/api/task/list.do',value)
    assert [t['id'] for t in filtered['data']['tasks'][0]['list']]==[3,4]
    assert len(value['data']['tasks'][0]['list'])==5


def test_retired_wallet_business_is_removed_before_sign_submission(client):
    from protocol.task_policy import filter_response
    activate({'task_keys':{'sign_wallet':'旧签到停用'}})
    result=filter_response('/api/mall/task/center/new/signBizList.do',{'data':[{'bizCode':'walletWelfare'},{'bizCode':'appstore'}]})
    assert result['data']==[{'bizCode':'appstore'}]


def test_failed_renewal_stays_suspended_after_the_old_cooldown(client):
    from execution_policy import renewal_candidates
    from datetime import datetime,timedelta
    activate({'suspend_failed_renewals':True})
    stamp=datetime.fromisoformat(now()).timestamp()
    account={'id':1,'status':'pending_login','panel_status':'online','credentials':encrypt({'_pool':'https://owned.test'})}
    latest={1:{'status':'failed','error':'HTTP 404','finished_at':(datetime.fromisoformat(now())-timedelta(days=2)).isoformat()}}
    assert renewal_candidates([account],latest,stamp)==[]


def test_paused_source_handler_never_reads_account_or_calls_provider(client):
    from protocol.source_bridge import run_source
    activate({'task_keys':{'reservation_rewards':'缺少本人原生设备'}})
    api=Mock()
    result=run_source(api,'reservation_rewards')
    assert result['removed'] and result['status']=='skipped'
    api.pm_points.assert_not_called()


def test_unavailable_rows_keep_an_audit_without_becoming_successes(client):
    from protocol.task_policy import prune_result
    activate({'remove_manual_subtasks':True})
    original={'status':'partial','results':[{'name':'贷款','status':'blocked','reason_kind':'manual','reason':'需要本人借款'}]}
    result=prune_result(original)
    assert result['status']=='skipped' and result['results']==[] and result['excluded_count']==1
    assert result['excluded_tasks'][0]['name']=='贷款'
    assert prune_result(result)==result
    nested={'status':'partial','results':[{'status':'partial','results':[
        {'name':'阅读','status':'completed'}, {'name':'邀请','status':'blocked','reason_kind':'manual'}]}]}
    pruned=prune_result(nested)
    assert pruned['status']=='completed' and prune_result(pruned)==pruned
    failure={'status':'failed','results':[{'name':'查询','status':'failed','reason':'ReadTimeout'}]}
    assert prune_result(failure)==failure
    unavailable={'status':'partial','results':[{'name':'会员券','status':'blocked','reason':'需要对应会员等级或本人 SIM 验证'},
                                             {'name':'未知组件','status':'blocked','reason':'活动配置未匹配到已知任务组件，保留待适配'}]}
    assert prune_result(unavailable)['results']==[]


def test_suspensions_can_be_restored_by_removing_the_policy_file(client):
    import store
    from protocol.task_policy import step_reason
    activate({'steps':{'source.browser_video':'没有设备'}})
    assert step_reason('source.browser_video')
    (store.DATA/'task-exclusions.json').unlink()
    assert step_reason('source.browser_video')==''


def test_known_unavailable_account_is_skipped_without_network_and_history_is_untouched(client,monkeypatch):
    import engine
    from adapter import import_accounts
    from execution_policy import record_verification_hold
    activate({'skip_unavailable_accounts':True})
    raw={'openid':'owned','vivotoken':'token'}
    import_accounts('live',[raw])
    with db('live') as conn:
        aid=conn.execute('SELECT id FROM accounts').fetchone()[0]
        conn.execute('UPDATE accounts SET points=100 WHERE id=?',(aid,))
        conn.execute("INSERT INTO point_history(account_id,delta,reason,created_at) VALUES(?,20,'原收益',?)",(aid,now()))
        conn.execute("INSERT INTO batches VALUES('paused','paused','full','running',1,1,?,NULL)",(now(),))
        rid=conn.execute("INSERT INTO runs(batch_id,account_id,status) VALUES('paused',?,'running')",(aid,)).lastrowid
    record_verification_hold('live',aid,raw,{'message':'需要验证','diagnostics':{'reason_kind':'account_verification'}})
    provider=Mock();monkeypatch.setattr(engine,'live_events',provider)
    engine.run_one('live',rid,'full',proxy_override={})
    provider.assert_not_called()
    with db('live') as conn:
        assert conn.execute('SELECT status FROM runs WHERE id=?',(rid,)).fetchone()[0]=='skipped'
        assert conn.execute('SELECT points FROM accounts WHERE id=?',(aid,)).fetchone()[0]==100
        assert conn.execute('SELECT sum(delta) FROM point_history').fetchone()[0]==20
