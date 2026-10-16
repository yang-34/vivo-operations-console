# Bundled vivo protocol reference; see source-manifest.json.
"""Standalone vivo activity protocols, discovery and resumable free tasks."""
import hashlib
import hmac
import json
from copy import deepcopy
from urllib.parse import unquote
from .compat import timezone
from .added_common import Journal, obj, report, rows
from .protocol_common import ActionRequired, ProtocolError, amount
from .sdk import StepResult, TaskSpec, UnknownWriteResult
POINT = 'https://pointh5.vivo.com.cn'
WUKONG = 'https://wukongapi.vivo.com.cn'
ACTIVITY = 'https://pointactivity.vivo.com.cn'
TREASURE = 'https://welfareh5.vivo.com.cn'
SITES = {'monopoly': 'wk2202240772c215', 'quiz': 'wk240410b3206fb4', 'golden': 'wk241209756168f3', 'double11': 'wk2609233beab7e8', 'weekly': 'wk261008324ff1c9', 'powerball': 'wk2305190f212481'}
KB = {'以下属于圆舞曲的是': '《蓝色多瑙河》', '下列不属于旅游主题公园的是？': '长江三峡', '名画《清明上河图》描绘的是当年何处近郊清明时节社会各阶层的生活景象': '汴京', '一天中气温最低出现在': '日出前后', '中国的四大菜系不包括哪一个？': '湘菜'}
TASKS = (TaskSpec('activity_catalog', '查询全部独立玩法与本期配置'), TaskSpec('quiz', '积分答题完整一轮', True, False, '答题存在入场积分费用，须在兑换与消耗中按当前报价确认。'), TaskSpec('quiz_rewards', '领取答题榜单已达标奖励', True), TaskSpec('monopoly_tasks', '大富翁任务与普通浏览', True), TaskSpec('monopoly_rewards', '领取大富翁已达标奖励', True), TaskSpec('weekly_tasks', '周周幸运抽任务与真实关系结算', True), TaskSpec('golden_tasks', '金蛋活动任务与普通浏览', True), TaskSpec('lottery_records', '查询独立抽奖机会和中奖记录'), TaskSpec('koicarp_rewards', '领取已开奖锦鲤奖励', True), TaskSpec('double11_remind', '查询双11领券场次与提醒状态'), TaskSpec('charity_status', '查询公益项目与本人捐赠记录'), TaskSpec('guess_status', '查询竞猜选项、额度与参与记录'), TaskSpec('treasure_status', '查询积分夺宝与本人记录'), TaskSpec('powerball_status', '查询彩票活动与本人参与统计'))

def walk(value, depth=0):
    if depth > 15:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child, depth + 1)
    elif isinstance(value, list):
        for child in value[:1000]:
            yield from walk(child, depth + 1)

class Activities:

    def __init__(self, client):
        self.client, self.configs = (client, {})
        client.http.hosts |= {'pointactivity.vivo.com.cn', 'welfareh5.vivo.com.cn', 'mshopact.vivo.com.cn'}

    def call(self, host, path, params=None, *, writes=False, method='GET', headers=None, site=None):
        return self.client._call(host, path, params=params or {}, method=method, writes=writes, pkg='com.vivo.space', with_vivo_account=True, cookie_profile='wukong_appointment' if host == WUKONG else None, common=False, referer='https://zhan.vivo.com.cn/point/' + (site or SITES['monopoly']), extra_headers=headers).get('data')

    def config(self, family):
        if family in self.configs:
            return self.configs[family]
        site = SITES[family]
        data = obj(self.call(WUKONG, '/wukongtopic/site/online/query', {'siteId': site, 'noTimestamp': True}, site=site))
        raw = data.get('config')
        try:
            config = json.loads(unquote(raw)) if isinstance(raw, str) else raw
        except ValueError as exc:
            raise ProtocolError('悟空活动配置格式未识别。') from exc
        if not isinstance(config, (list, dict)):
            raise ActionRequired('官方未发布当前玩法配置。', reason_kind='unavailable')
        names = {'quiz': ('question',), 'golden': ('goldenegg', 'lottery'), 'double11': ('lottery',), 'weekly': ('carveup', 'luck'), 'powerball': ('powerball',), 'monopoly': ('monopoly',)}[family]
        candidates = []
        for node in walk(config):
            kind = str(node.get('name') or node.get('componentName') or node.get('component') or '').lower()
            if any((n in kind for n in names)):
                candidates.extend((str(n['activityId']) for n in walk(node) if n.get('activityId')))
        published = {str(n['activityInfo']['activityId']) for n in walk(config) if isinstance(n.get('activityInfo'), dict) and n['activityInfo'].get('activityId')}
        identifiers = set(candidates) or published or {str(n['activityId']) for n in walk(config) if n.get('activityId')}
        if len(identifiers) != 1:
            raise ActionRequired('当前活动配置没有唯一的玩法标识，未套用过期活动号。', reason_kind='unavailable')
        secret = next((str(n['secretKey']) for n in walk(config) if n.get('secretKey')), '')
        result = {'activityId': identifiers.pop(), 'siteId': site, 'secret': secret, 'config': config}
        self.configs[family] = result
        return result

    def params(self, family):
        c = self.config(family)
        return {k: c[k] for k in ('activityId', 'siteId')}

    def w(self, family, path, extra=None, *, writes=False, method='GET', headers=None):
        return self.call(WUKONG, path, {**self.params(family), **(extra or {})}, writes=writes, method=method, headers=headers, site=SITES[family])

    def available(self, family):
        extra = {'groupId': 1} if family == 'double11' else {}
        return obj(self.w(family, '/wukongapc/lottery/user/available/query', extra))

    def draws(self, family):
        return self.w(family, '/wukongapc/lottery/win/list')

    def draw(self, family):
        p = self.params(family)
        return self.call(WUKONG, '/wukongapc/lottery/draw', {**p, 'topicId': p['siteId'], 'extend': {'hostUrl': 'https://mshopact.vivo.com.cn/vivospace/' + p['siteId'] + '?h5JumpBackFlag=1', 'locationAuthorizedDTO': {'hasUserLocation': False, 'province': '', 'city': ''}}}, writes=True, method='POST', site=p['siteId'])

    def weekly_available(self):
        return obj(self.w('weekly', '/wukongapc/carveupLuck/lucky/available'))

    def weekly_exchange(self, count=1):
        if type(count) is not int or not 1 <= count <= 1000:
            raise ProtocolError('幸运码领取数量不在本轮范围。')
        return self.w('weekly', '/wukongapc/carveupLuck/lucky/exchange', {'count': count, 'hasUserLocation': False, 'province': '', 'city': ''}, writes=True, method='POST')

    def monopoly(self, suffix):
        return self.w('monopoly', '/wukongapc/api/monopoly/' + suffix)

    def monopoly_write(self, suffix, body, *, ticket=''):
        return self.w('monopoly', '/wukongapc/api/monopoly/' + suffix, body, writes=True, method='POST', headers={'X-Captcha-Ticket': ticket} if ticket else None)

    def koi(self, suffix, body=None, writes=False):
        return self.call(POINT, '/api/koicarp/' + suffix + '.do', body, writes=writes, method='POST' if writes else 'GET')

    def guess(self):
        return obj(obj(self.call(POINT, '/api/nmall/home/explicit/list.do')).get('guessActivity'))

    def projects(self):
        data = self.call(POINT, '/api/welfare/project/list.do')
        if not isinstance(data, list):
            raise ProtocolError('公益项目列表不完整。')
        return rows(data)

    def powerball(self, suffix):
        return self.call(ACTIVITY, '/api/powerball/' + suffix + '.do', {'activityId': self.config('powerball')['activityId']})

    def treasure(self, suffix, extra=None, *, writes=False):
        return self.call(TREASURE, '/h5/point/db/api/' + suffix, {'activityId': 59, **(extra or {})}, writes=writes)

    def quiz(self, suffix, extra=None, *, writes=False, method='GET', headers=None):
        return self.w('quiz', '/wukongapc/question/' + suffix, extra, writes=writes, method=method, headers=headers)

    def quiz_submit(self, question_id, answer, token):
        cfg = self.config('quiz')
        if not cfg['secret']:
            raise ProtocolError('当前答题配置没有签名材料。')
        aladdin = self.w('quiz', '/wukongapc/activity/config/sign/token', {'apiName': 'submitAnswer'})
        if not isinstance(aladdin, str) or not aladdin:
            raise ProtocolError('答题签名票据未返回。')
        params = {**self.params('quiz'), 'questionId': question_id, 'userAnswer': answer, 'answerToken': token}
        signed = {**params, 'apiName': 'submitAnswer', 'aladdinToken': aladdin}
        signature = hmac.new(cfg['secret'].encode(), ''.join((f'{k}{v}' for k, v in sorted(signed.items()))).encode(), hashlib.sha1).hexdigest()
        return self.quiz('user/submit', params, writes=True, method='POST', headers={'aladdin_sign': f'apiName=submitAnswer&signature={signature}&aladdinToken={aladdin}'})

def run_quiz(adapter, client, context, snapshot, *, allow_paid=False, max_cost=0, api=None, ticket=''):
    api, journal = (api or Activities(client), Journal(context, 'vivo_quiz_journal'))
    info = obj(api.quiz('user/info'))
    if info.get('deductPoint') is None:
        raise ActionRequired('平台未明确本轮答题费用，默认不启动。', reason_kind='consumption')
    price = amount(info['deductPoint'])
    if not allow_paid and price:
        raise ActionRequired(f'本轮答题入场需要{price}积分，请在兑换与消耗模块确认。', reason_kind='consumption')
    scopes = deepcopy(context.credentials.get('scopes', {}))
    key, day = ('_taskflow_vivo_quiz', timezone.localdate().isoformat())
    saved = obj(scopes.get(key))
    before = amount(info.get('tdTotalCount'))
    if saved.get('date') != day:
        saved = {'date': day, 'knowledge': saved.get('knowledge', {})}
    if not saved.get('token'):
        if amount(info.get('remainTimes')) == 0:
            return StepResult('skipped', '今天的答题机会已用完', snapshot)
        if price > max_cost:
            raise ActionRequired('答题费用高于本次确认上限，未开始。')
        wallet_before = adapter.profile(client).snapshot['points']
        result = obj(journal.submit(day + ':start', lambda: api.quiz('user/start', writes=True, method='POST', headers={'X-Captcha-Ticket': ticket} if ticket else None)))
        if not result.get('answerToken'):
            raise UnknownWriteResult
        saved.update(token=result['answerToken'], before=before, wallet_before=wallet_before, entry_cost=price, earned=0)
        context.persist_scopes({**context.credentials.get('scopes', {}), key: saved})
        if wallet_before - adapter.profile(client).snapshot['points'] != price:
            raise UnknownWriteResult
        journal.confirm(day + ':start')
        client.http.writing = False
    question = obj(api.quiz('user/question/pull', {'answerToken': saved['token']}))
    qid = question.get('questionId')
    if not qid:
        if before > saved.get('before', before):
            saved.pop('token', None)
            context.persist_scopes({**context.credentials.get('scopes', {}), key: saved})
            return StepResult('success', '本轮答题完成，累计作答数已回读', {**snapshot, **adapter.profile(client).snapshot})
        raise ProtocolError('答题状态没有当前题目或已完成依据。')
    options = rows(question.get('options'))
    if not options or any((o.get('key') is None for o in options)):
        raise ProtocolError('当前题目选项不完整。')
    question_hash = hashlib.sha256(str(question.get('content', '')).encode()).hexdigest()
    knowledge = saved.setdefault('knowledge', {})
    answer_text = knowledge.get(question_hash) or KB.get(str(question.get('content', '')))
    selected = next((o for o in options if o.get('value') == answer_text), options[0])
    rid = day + ':' + str(qid)
    receipt = obj(journal.submit(rid, lambda: api.quiz_submit(qid, selected['key'], saved['token']), before=before))
    after = obj(api.quiz('user/info'))
    if amount(after.get('tdTotalCount')) <= before:
        raise UnknownWriteResult
    right = receipt.get('rightAnswer')
    correct = next((o.get('value') for o in options if o.get('key') == right), None)
    if correct:
        knowledge[question_hash] = correct
    more = amount(receipt.get('remainQuesNum')) > 0
    saved['earned'] = amount(saved.get('earned', 0)) + amount(receipt.get('gotPoints', 0))
    current = adapter.profile(client).snapshot
    if not more and saved['wallet_before'] - current['points'] + saved['earned'] != saved['entry_cost']:
        raise UnknownWriteResult
    if not more:
        saved.pop('token', None)
    context.persist_scopes({**context.credentials.get('scopes', {}), key: saved})
    journal.confirm(rid)
    client.http.writing = False
    return StepResult('success', f"本题已提交并回读；答对{amount(after.get('tdRightCount'))}题，今日获得{amount(after.get('tdPoints'))}积分", {**snapshot, **current}, continue_after=1 if more else None)

def execute(adapter, client, context, key, snapshot):
    if key == 'quiz':
        return run_quiz(adapter, client, context, snapshot)
    api, entries = (Activities(client), [])
    if key == 'activity_catalog':
        for family in SITES:
            try:
                api.config(family)
                entries.append({'name': family, 'status': 'success', 'message': '本期活动配置已读取'})
            except ActionRequired as exc:
                entries.append({'name': family, 'status': 'action_required', 'message': exc.message})
        return report(snapshot, entries, '独立玩法当前配置已逐项核对')
    if key == 'lottery_records':
        for family in ('golden', 'double11'):
            available = api.available(family)
            api.draws(family)
            count = amount(available.get('freeAvaCnt')) + amount(available.get('taskAvaCnt'))
            entries.append({'name': family, 'status': 'success', 'message': f'当前免费与任务机会合计{count}次'})
        weekly = api.weekly_available()
        entries.append({'name': '周周幸运抽', 'status': 'success', 'message': f"可用{amount(weekly.get('availableCount'))}次，已有{amount(weekly.get('luckyNoAmount'))}个幸运码"})
        return report(snapshot, entries)
    if key == 'guess_status':
        state = api.guess()
        return StepResult('success' if state else 'skipped', '当前竞猜选项与参与状态已读取' if state else '当前没有竞猜活动', snapshot)
    if key == 'charity_status':
        projects = api.projects()
        api.call(POINT, '/api/welfare/user/info.do')
        return StepResult('success', f'已查询{len(projects)}个公益项目及本人记录', snapshot)
    if key == 'powerball_status':
        api.powerball('baseInfo')
        api.powerball('statistic')
        return StepResult('success', '彩票活动与本人参与统计已读取', snapshot)
    if key == 'treasure_status':
        from .vivo_source_flows import crowdfunding_config
        for site, aid in crowdfunding_config(client):
            params = {'activityId': aid, 'siteId': site}
            config = obj(api.call(WUKONG, '/wukongapc/crowdfunding/config/query', params, site=site))
            state = obj(api.call(WUKONG, '/wukongapc/crowdfunding/lucky/available', params, site=site))
            if 'availableCount' not in state or 'luckyNoAmount' not in state:
                raise ProtocolError('当前夺宝未返回完整的本人免费次数和持有码。')
            entries.append({'name': '当期积分夺宝', 'status': 'success', 'message': f"活动状态{config.get('activityStatus')}；免费次数{amount(state['availableCount'])}，本人持有码{amount(state['luckyNoAmount'])}"})
        if not entries:
            return StepResult('skipped', '当前官方入口未发布积分夺宝活动；未请求退役域名或历史活动59', snapshot, diagnostics={'skip_kind': 'unavailable'})
        return report(snapshot, entries, '当前夺宝配置与本人记录已回读；未兑换积分次数')
    from .vivo_activity_tasks import execute as execute_tasks
    return execute_tasks(adapter, client, context, api, key, snapshot)
