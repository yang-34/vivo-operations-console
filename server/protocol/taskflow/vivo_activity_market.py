# Bundled vivo protocol reference; see source-manifest.json.
"""Standalone games use current rules and one explicitly confirmed operation."""
import hashlib
import time
from copy import deepcopy
from .added_common import obj, rows, title, yes
from .confirmed_operations import collect_catalog, item
from .protocol_common import ActionRequired, ProtocolError, amount
from .sdk import StepResult, UnknownWriteResult
from .vivo_activities import ACTIVITY, POINT, Activities, walk
from .vivo_activity_tasks import coupon_state

def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]

def available_count(data):
    return amount(data.get('freeAvaCnt')) + amount(data.get('taskAvaCnt'))

def dice_count(data):
    for field in ('diceNum', 'diceCount', 'remainDiceCount', 'availableDiceNum'):
        if field in data:
            return amount(data[field])
    raise ProtocolError('大富翁没有提供明确可用骰子数。')

def catalog(provider, target=None):
    api, balance = (Activities(provider.client), provider.balance())

    def offers(family):
        out = []
        if family in ('golden', 'double11'):
            config, state = (api.config(family), api.available(family))
            count = available_count(state)
            out.append(item(f'vivo:{family}-draw', ('金蛋' if family == 'golden' else '双11') + ' · 免费机会单次抽取', 1, '免费活动机会', count, {'kind': family, 'activityId': config['activityId'], 'siteId': config['siteId']}, eligible=count > 0, reason='' if count else '当前没有已持有的免费或任务机会', rules='仅使用本人已持有的一次活动机会，不购买机会、不消耗积分；回读次数及余额。', terms={'quantity': 1, 'points_cost': 0}))
        elif family == 'weekly':
            c, state = (api.config(family), api.weekly_available())
            count = amount(state.get('availableCount'))
            out.append(item('vivo:weekly-code', '周周幸运抽 · 兑换一个幸运码', 1, '免费活动机会', count, {'kind': family, 'activityId': c['activityId'], 'siteId': c['siteId']}, eligible=count > 0, reason='' if count else '当前没有可兑换的免费机会', rules='只兑换一个幸运码，核对机会减少1、持有码增加1；不消费积分。', terms={'quantity': 1, 'points_cost': 0}))
        elif family == 'koi':
            state = obj(api.koi('init'))
            info = obj(state.get('koicarpActivityInfo'))
            for points in info.get('points', []):
                points = amount(points)
                if points <= 0:
                    continue
                out.append(item(f'vivo:koi-{points}', f'锦鲤 · {points}积分档参与一次', points, 'vivo 积分', balance, {'kind': family, 'point': points, 'version': info.get('version')}, eligible=str(info.get('status')) == '1', reason='' if str(info.get('status')) == '1' else '活动当前未开放', rules='按当前活动配置的所选档位参与一次，可能未获奖励；不连续投入。', terms={'quantity': 1, 'version': info.get('version')}))
        elif family == 'guess':
            state = api.guess()
            if not state:
                return []
            points = amount(state.get('minChip'))
            open_now = state.get('startTime', 0) <= time.time() * 1000 < state.get('endTime', 0)
            eligible = open_now and (not yes(state.get('isChipIn'))) and (amount(state.get('currentTimes')) < amount(state.get('limitTimes')))
            for option in rows(state.get('options')):
                if option.get('code') is None:
                    continue
                out.append(item(f"vivo:guess-{state['id']}-{digest(option['code'])}", title(state.get('name')) + ' · ' + title(option.get('name')), points, 'vivo 积分', balance, {'kind': family, 'activityId': state['id'], 'optionCode': option['code'], 'points': points}, eligible=eligible, reason='' if eligible else '当前已参与、场次结束或资格不足', rules='本人选择该选项，以当前最低额度参与一次；活动结算前不更改选项，不连续投入。', terms={'quantity': 1, 'endTime': state.get('endTime'), 'option': title(option.get('name'))}))
        elif family == 'charity':
            for project in api.projects():
                if not project.get('key'):
                    continue
                choices = [amount(p) for p in project.get('recommendNumbers', []) if str(p).isdigit() and int(p) > 0]
                contribution = min(choices) if choices else 1
                out.append(item('vivo:charity-' + digest(project['key']), title(project.get('title')) + f' · 捐赠{contribution}积分', contribution, 'vivo 积分', balance, {'kind': family, 'key': project['key'], 'points': contribution}, eligible=project.get('completeStatus') == 0, reason='' if project.get('completeStatus') == 0 else '项目已完成或未开放', rules='将所示积分捐赠给这个指定公益项目，捐赠后不返还；回读个人捐赠次数、积分及余额。', terms={'quantity': 1}))
        elif family == 'powerball':
            state = obj(api.powerball('baseInfo'))
            info = obj(state.get('powerBallActivityVO'))
            cost = amount(info.get('pointsPerNote'))
            count = amount(state.get('todayOrPeriodBetNumber'))
            limit = int(info.get('limitNotes', 0))
            eligible = yes(state.get('isLogin')) and info.get('activityStartTime', 0) <= time.time() * 1000 < min(info.get('activityEndTime', 0), info.get('betEndTime', 0)) and (limit == -1 or count < limit)
            out.append(item('vivo:powerball-' + str(info.get('activityId')), title(info.get('title'), '彩票活动') + ' · 一注', cost, 'vivo 积分', balance, {'kind': family, 'gameId': info.get('activityId'), 'betNum': 1}, eligible=eligible, reason='' if eligible else '当前场次已截止或次数不足', rules='仅参与当前一期的一注，可能未中奖；需要官方安全验证时由本人完成。', terms={'quantity': 1, 'period': info.get('curPeriod'), 'betEndTime': info.get('betEndTime')}))
        elif family == 'monopoly':
            config, state = (api.config(family), obj(api.monopoly('user/info')))
            count = dice_count(state)
            out.append(item('vivo:monopoly-roll', '大富翁 · 使用一个现有骰子', 1, '已有骰子', count, {'kind': family, 'activityId': config['activityId'], 'siteId': config['siteId'], 'skillId': 0}, eligible=count > 0, reason='' if count else '当前没有骰子', rules='只使用一个已有骰子和普通移动，不购买骰子、不使用付费技能。所获待领奖励单独回读。', terms={'quantity': 1, 'points_cost': 0}))
            pending = provider.credentials.get('scopes', {}).get('_taskflow_vivo_monopoly_prize', {})
            if pending.get('token') and str(pending.get('activityId')) == config['activityId']:
                out.append(item('vivo:monopoly-prize-' + digest(pending['token']), '领取本次大富翁掷骰子奖励', 0, 'vivo 积分', balance, {'kind': 'monopoly_prize', 'activityId': config['activityId'], 'token_ref': digest(pending['token'])}, rules='仅使用本账号本次掷骰子下发的奖励票据，不购买机会、不消费积分。', terms={'quantity': 1}))
        elif family == 'quiz':
            config, state = (api.config(family), obj(api.quiz('user/info')))
            price, remaining = (amount(state.get('deductPoint')), amount(state.get('remainTimes')))
            out.append(item('vivo:quiz-round', '积分答题 · 完整一轮', price, 'vivo 积分', balance, {'kind': family, 'activityId': config['activityId'], 'siteId': config['siteId']}, eligible=remaining > 0, reason='' if remaining else '今日机会已用完', rules='按当前入场费开启一轮。题库命中优先，未知题使用当前首个选项并学习官方返回的正确答案；不保证答对或收益。一轮仅扣一次入场费，逐题提交并回读实际结果。', terms={'quantity': 1, 'question_count': state.get('quesNumsEachTime'), 'entry_cost': price}))
        elif family == 'treasure':
            state = obj(api.treasure('getActivityInfo'))
            awards = api.treasure('getAwardList')
            for award in rows(awards if isinstance(awards, list) else obj(awards).get('list')):
                aid = award.get('awardId') or award.get('id')
                point = award.get('minPoint')
                if aid is None or point is None:
                    continue
                out.append(item('vivo:treasure-' + str(aid), title(award.get('name'), '夺宝奖品') + ' · 最低额度一份', point, 'vivo 积分', balance, {'kind': family, 'awardId': aid, 'point': point}, eligible=str(state.get('status')) == '1', reason='' if str(state.get('status')) == '1' else '活动未开放', rules='仅投入当前奖品的一份最低额度，可能未中奖；原官方域不可用时不改用静态资源域。', terms={'quantity': 1}))
        elif family == 'remind':
            data = coupon_state(api)
            tabs = [t for t in rows(data.get('couponGrabTabList')) if int(t.get('startTime', 0)) > time.time() * 1000]
            if tabs:
                tab = min(tabs, key=lambda t: int(t['startTime']))
                coupon = next((r for r in walk(data) if r.get('couponUuid')), {})
                if coupon:
                    out.append(item('vivo:coupon-remind', '为下一场双11领券开启提醒', 0, 'vivo 积分', balance, {'kind': family, 'activityId': data['_activityId'], 'tabId': tab['id'], 'couponUuid': coupon['couponUuid']}, rules='只为所显示的下一场领券设置本账号提醒，不下单、不消费积分。', terms={'quantity': 1, 'startTime': tab['startTime']}))
        return out
    families = ('golden', 'double11', 'weekly', 'koi', 'guess', 'charity', 'powerball', 'monopoly', 'treasure', 'remind', 'quiz')
    if target:
        family = next((f for f in families if target.startswith('vivo:' + f + '-')), None)
        if target == 'vivo:coupon-remind':
            family = 'remind'
        return [r for r in offers(family) if r['key'] == target] if family else []
    return collect_catalog(provider, [(family, lambda f=family: offers(f)) for family in families])

def execute(provider, proposal):
    api, client = (Activities(provider.client), provider.client)
    body, cost = (proposal['payload'], amount(proposal['cost']))
    kind = body.get('kind')
    validation = getattr(provider, 'confirmation_input', {})
    ticket = validation.get('ticket', '')
    before = provider.balance()
    if proposal['currency'] == 'vivo 积分' and before < cost:
        raise ActionRequired('当前积分不足。')
    if kind in ('golden', 'double11'):
        count = available_count(api.available(kind))
        if count < 1:
            raise ActionRequired('当前没有免费活动机会。')
        api.draw(kind)
        if available_count(api.available(kind)) != count - 1 or provider.balance() < before:
            raise UnknownWriteResult
    elif kind == 'weekly':
        state = api.weekly_available()
        available, held = (amount(state.get('availableCount')), amount(state.get('luckyNoAmount')))
        if available < 1:
            raise ActionRequired('当前没有可兑换的免费机会。')
        result = api.weekly_exchange()
        after = api.weekly_available()
        if not isinstance(result, list) or len(result) != 1 or amount(after.get('availableCount')) != available - 1 or (amount(after.get('luckyNoAmount')) != held + 1) or (provider.balance() < before):
            raise UnknownWriteResult
    elif kind == 'guess':
        old = api.guess()
        api.call(POINT, '/api/activity/guess/chipIn.do', {**{k: body[k] for k in ('activityId', 'points', 'optionCode')}, 'scene': 'guess', 'ticket': ticket, 'constId': validation.get('const_id', '')}, writes=True, method='POST')
        new = api.guess()
        if not yes(new.get('isChipIn')) or amount(new.get('currentTimes')) <= amount(old.get('currentTimes')) or before - provider.balance() != cost:
            raise UnknownWriteResult
    elif kind == 'charity':
        old = obj(api.call(POINT, '/api/welfare/user/info.do'))
        api.call(POINT, '/api/welfare/contribute.do', {'key': body['key'], 'points': cost}, writes=True, method='POST')
        new = obj(api.call(POINT, '/api/welfare/user/info.do'))
        if amount(new.get('contributePoints')) - amount(old.get('contributePoints')) != cost or amount(new.get('contributeTimes')) != amount(old.get('contributeTimes')) + 1 or before - provider.balance() != cost:
            raise UnknownWriteResult
    elif kind == 'powerball':
        old = obj(api.powerball('baseInfo'))
        validation = getattr(provider, 'confirmation_input', {})
        api.call(ACTIVITY, '/api/power/ball/bet', {'gameId': body['gameId'], 'betNum': 1, 'scene': 'powerBall', 'ticket': validation.get('ticket', ''), 'constId': validation.get('const_id', '')}, writes=True, method='POST')
        after = obj(api.powerball('baseInfo'))
        if amount(after.get('todayOrPeriodBetNumber')) != amount(old.get('todayOrPeriodBetNumber')) + 1 or before - provider.balance() != cost:
            raise UnknownWriteResult
    elif kind == 'koi':
        old = obj(api.koi('init'))
        validation = getattr(provider, 'confirmation_input', {})
        api.koi('betting', {'noLoading': True, 'acceptError': True, 'point': body['point'], 'version': body['version'], 'scene': 'koicarp', 'ticket': validation.get('ticket', ''), 'constId': validation.get('const_id', '')}, writes=True)
        new = obj(api.koi('init'))
        profit = amount(obj(new.get('userInfo')).get('totalProfitPoint')) - amount(obj(old.get('userInfo')).get('totalProfitPoint'))
        if before - provider.balance() + profit != cost:
            raise UnknownWriteResult
    elif kind == 'monopoly':
        count = dice_count(obj(api.monopoly('user/info')))
        if count < 1:
            raise ActionRequired('当前没有可用骰子。')
        result = obj(api.monopoly_write('dice/roll', {'skillId': 0}, ticket=ticket))
        if dice_count(obj(api.monopoly('user/info'))) != count - 1 or provider.balance() < before:
            raise UnknownWriteResult
        if result.get('token'):
            scopes = deepcopy(provider.credentials.get('scopes', {}))
            scopes['_taskflow_vivo_monopoly_prize'] = {'activityId': body['activityId'], 'token': result['token'], 'topicId': result.get('topicId', 0)}
            provider.credentials['scopes'] = scopes
    elif kind == 'monopoly_prize':
        saved = provider.credentials.get('scopes', {}).get('_taskflow_vivo_monopoly_prize', {})
        if not saved.get('token') or digest(saved['token']) != body['token_ref'] or str(saved.get('activityId')) != str(body['activityId']):
            raise ActionRequired('本次大富翁奖励票据已变化。')
        result = api.monopoly_write('draw/lottery', {'token': saved['token'], 'topicId': saved.get('topicId', 0)}, ticket=ticket)
        if not isinstance(result, dict) or not any((result.get(k) for k in ('prizeId', 'rewardId', 'prize', 'reward'))) or provider.balance() < before:
            raise UnknownWriteResult
        provider.credentials['scopes'].pop('_taskflow_vivo_monopoly_prize', None)
    elif kind == 'treasure':
        old = api.treasure('getRecordList')
        api.treasure('invest', {'awardId': body['awardId'], 'point': cost, 'ticket': ticket}, writes=True)
        if api.treasure('getRecordList') == old or before - provider.balance() != cost:
            raise UnknownWriteResult
    elif kind == 'remind':
        api.call('https://mshopact.vivo.com.cn', '/couponGrab/updateRemind', {**{k: body[k] for k in ('activityId', 'tabId', 'couponUuid')}, 'isRemind': 1}, writes=True)
        after = coupon_state(api)
        flags = [n for n in walk(after) if n.get('id') == body['tabId'] and 'isRemind' in n]
        if flags and (not any((yes(n['isRemind']) for n in flags))):
            raise UnknownWriteResult
    else:
        raise ProtocolError('未知的独立玩法操作。')
    client.http.writing = False
    return StepResult('success', '本次所选玩法操作与相应次数、记录或积分变化已回读', {**provider.snapshot, 'points': provider.balance()}, {'scopes': provider.credentials.get('scopes', {})})
