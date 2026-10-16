# Bundled vivo protocol reference; see source-manifest.json.
"""任务中心（积分体系）完整接口客户端。

覆盖源码实证的**三个后端**：

| 后端 | 作用 | 来源 |
|---|---|---|
| `pointh5.vivo.com.cn` | 任务中心主体（任务列表/详情/领奖/App签到业务/积分/会员等级） | 积分 H5 SPA `app.78fc7baea7ebaef61992.js` |
| `pointwap.vivo.com.cn` | 悟空签到页的任务（`/api/task/ec/*`） | `wukong.fdac1008.js` |
| `pointsdk.vivo.com.cn` | 任务许愿 `/web/task/wish` | 同上 |

任务分类（实测 `/api/task/list.do`）：

| group.type | 名称 |
|---|---|
| `"0"` | 每日任务 |
| `"1"` | 活动任务 |
| `"2"` | 新手任务 |
| `"5"` | 精选任务 |
| `"6"` | 月度任务 |

「App 侧任务」= `/api/mall/task/center/new/signBizList.do` 返回的 **11 个 vivo App 的签到任务**。

⚠️ 鉴权：本模块用 **cookie**（`vvc_openid` + `vvc_r` + `vvc_pn` + `vvc_imei`）。

* ``vvc_pn`` 会被服务端当作**客户端包名**校验，因此分两套：
  - 任务/积分类（pointh5、pointwap）用 ``vvc_pn=miniprogram``；
  - ``sign.vivo.com.cn`` 的签到要求**真实 vivo 包名**或不带。
* ``vvc_imei`` 是**设备标识**，签到写接口必需。缺失时 ``dosign`` 返回
  ``1050 账户异常``（文案误导，实为设备校验未过）。详见 :mod:`client.device`。
"""
from __future__ import annotations
import json
import time
from typing import Any
POINTH5 = 'https://pointh5.vivo.com.cn'
POINTWAP = 'https://pointwap.vivo.com.cn'
POINTSDK = 'https://pointsdk.vivo.com.cn'
SIGNHOST = 'https://sign.vivo.com.cn'
UA_MINIPROGRAM = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.49(0x18003124) NetType/WIFI Language/zh_CN miniProgram'
H5_REFERER = 'https://zhan.vivo.com.cn/point/wk2207264eeaa8ec'
TASK_GROUPS = {'0': '每日任务', '1': '活动任务', '2': '新手任务', '5': '精选任务', '6': '月度任务'}
BIZ_PACKAGE = {'appstore': 'com.bbk.appstore', 'gamecenter': 'com.vivo.game', 'vivospace': 'com.vivo.space', 'browser': 'com.vivo.browser', 'videoPlayer': 'com.android.VideoPlayer', 'wallet': 'com.vivo.wallet', 'iTheme': 'com.bbk.theme', 'iMusic': 'com.android.bbkmusic', 'minigame': 'com.vivo.minigamecenter', 'quickapp': 'com.quickapp.center', 'vhome': 'com.vivo.vhome', 'vivop': ''}
BIZ_ACTIVITY_ID = {'vivop': 71, 'appstore': 23, 'gamecenter': 32, 'vivospace': 46, 'browser': 35, 'videoPlayer': 34, 'wallet': 37, 'iTheme': 69, 'iMusic': 130, 'minigame': 162, 'quickapp': 27, 'vhome': 50}
DEFAULT_PACKAGE = 'com.vivo.game'
SIGN_ALREADY_DONE = 10003
SIGN_NEED_DEVICE = 1050
SIGN_DEVICE_MISMATCH = 1018
SPA_ALREADY_SIGNED = 10002
DEFAULT_SITE = {'site_id': 'wk2207264eeaa8ec', 'belong_client': 'vivop', 'cherry_task_show': '1', 'source': '0'}

class TaskCenterApi:

    def _cookies(self, *, pkg: str | None=None, with_vivo_account: bool=False, with_device: bool=True) -> str:
        """组装 Cookie。

        :param pkg: ``vvc_pn`` 的取值 —— **必须是真实 vivo 包名**或 ``None``（不带）。
            实测 ``vvc_pn=miniprogram`` 会被 `sign.vivo.com.cn` / `pointsdk`
            判为"当前客户端不支持"（10001）。
        :param with_vivo_account: 是否附加 ``vivo_account_cookie_iqoo_*``
            （`sign/capital/pointInfo.do` 需要）。
        :param with_device: 是否附加设备标识 ``vvc_imei``。
            **默认开启** —— 缺少它时 ``sign/dosign.do`` 会返回 ``1050``
            （详见 :mod:`client.device`）。可通过
            ``config.json → device.attach_to_all_requests`` 关闭
            （关闭后仅签到接口携带）。
        """
        s = self.api.store.session
        if with_device and (not self.attach_device_everywhere):
            with_device = False
        parts = [f'vvc_openid={s.openid}', f'vvc_r={s.vivotoken}']
        if pkg:
            parts.append(f'vvc_pn={pkg}')
        if with_device:
            parts.append(f'vvc_imei={self.device.imei}')
        if with_vivo_account:
            parts.append(f'vivo_account_cookie_iqoo_openid={s.openid}')
            parts.append(f'vivo_account_cookie_iqoo_vivotoken={s.vivotoken}')
        return ';'.join(parts)

    def pkg_for(self, biz: str | None) -> str | None:
        """业务标识 → ``vvc_pn`` 包名；``vivop``（vivo+）返回 ``None``（不带 pn）。"""
        if not biz:
            return self.cfg.get('default_package') or DEFAULT_PACKAGE
        b = str(biz)
        if b in BIZ_PACKAGE:
            return BIZ_PACKAGE[b] or None
        if '.' in b:
            return b
        return b

    @staticmethod
    def ok(res: Any) -> bool:
        """任务族成功判据：``retcode == 0``；签到族：``code == 0``。"""
        if not isinstance(res, dict):
            return False
        if 'retcode' in res:
            try:
                return int(res['retcode']) == 0
            except (TypeError, ValueError):
                return False
        try:
            return int(res.get('code', -1)) == 0
        except (TypeError, ValueError):
            return False

    def task_list(self, *, task_type: str | None=None) -> dict[str, Any]:
        """``GET /api/task/list.do`` —— 全部任务，按分类分组。

        返回 ``{"retcode":0,"data":{"sysTime":..,"tasks":[{name,hasMore,type,list:[...]}]},"login":bool}``
        """
        p = {'type': task_type} if task_type is not None else {}
        return self._call(self.pointh5, '/api/task/list.do', params=p)

    def task_list_by_type(self, task_type: str) -> dict[str, Any]:
        """``GET /api/task/listByType.do`` —— 按分类展开（``task_type`` 取 TASK_GROUPS 的键）。"""
        return self._call(self.pointh5, '/api/task/listByType.do', params={'type': str(task_type)})

    def task_detail(self, task_id: Any) -> dict[str, Any]:
        """``GET /api/task/detail.do`` —— 任务详情（参数 ``id``）。"""
        return self._call(self.pointh5, '/api/task/detail.do', params={'id': task_id})

    def receive_point(self, task: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/task/receivePoint.do`` —— **领取任务积分（写操作）**。

        源码 chunk-3 的调用点：``{taskId: t.id, type: t.type, subType: t.subType, taskSource: t.taskSource}``。
        响应：``{"retcode":0,"data":{"rewardPoints":N,"totalPoints":M}}``；
        ``1072`` = 温度不足（源码 toast"温…"）。
        """
        params = {'taskId': task.get('id')}
        for k in ('type', 'subType', 'taskSource'):
            if task.get(k) is not None:
                params[k] = task[k]
        return self._call(self.pointh5, '/api/task/receivePoint.do', method='POST', params=params)

    def complete_web_task(self, task_key_id: Any) -> dict[str, Any]:
        """``POST /api/task/completeWebTask.do`` —— 完成网页任务（参数 ``taskKeyId``）。"""
        return self._call(self.pointh5, '/api/task/completeWebTask.do', method='POST', params={'taskKeyId': task_key_id})

    def refresh_store_task_code(self, task_id: Any) -> dict[str, Any]:
        """``GET /api/task/refreshStoreTaskCode.do`` —— 刷新门店任务取货码。"""
        return self._call(self.pointh5, '/api/task/refreshStoreTaskCode.do', params={'taskId': task_id})

    def get_phone_reward(self, params: dict[str, Any] | None=None) -> dict[str, Any]:
        """``POST /api/task/getPhoneReward.do`` —— 手机奖励。"""
        return self._call(self.pointh5, '/api/task/getPhoneReward.do', method='POST', params=params or {})

    def ec_receive_point(self, task: dict[str, Any], *, business_code: str | None=None) -> dict[str, Any]:
        """``POST /api/task/ec/receivePoint.do`` —— **领取任务积分（写操作）**。

        源码 `mod-1b33-taskpage.js` 的 ``getPoint``::

            {noLoading:true, from:"EC", taskId:t.id, businessCode:this.businessCode,
             taskSource:t.taskSource, type:t.type, subType:t.subType}

        ``retcode == 0`` 时 ``data.rewardPoints`` 是本次领到的积分数；
        ``1018`` 是账号风控（源码里显式跳过，不当失败处理）。
        """
        params: dict[str, Any] = {'noLoading': True, 'from': 'EC', 'taskId': task.get('id'), 'businessCode': business_code or task.get('businessCode') or 'vivop'}
        for k in ('taskSource', 'type', 'subType'):
            if task.get(k) is not None:
                params[k] = task[k]
        return self._call(self.pointh5, '/api/task/ec/receivePoint.do', method='POST', params=params)

    def vivop_complete(self, key_id: Any, sub_key_id: Any=None, business_code: str='vivop') -> dict[str, Any]:
        """``POST /api/task/vivop/complete.do`` —— **vivo+ 任务完成上报（写操作）**。

        源码 `mod-1b33-taskpage.js` 的 ``finishVivoplusTask``::

            {keyId, subKeyId, businessCode:"vivop"}
        """
        return self._call(self.pointh5, '/api/task/vivop/complete.do', method='POST', params={'keyId': key_id, 'subKeyId': sub_key_id, 'businessCode': business_code})
    ROUTE_SIGN = 'sign'
    ROUTE_AD = 'ad'
    ROUTE_WEB = 'web'
    ROUTE_WISH = 'wish'

    def task_center_list(self) -> list[dict[str, Any]]:
        """全部任务拍平成一个列表（等价于 :meth:`flatten` 加一次 ``task_list()``）。

        每项额外带 ``_group``（分类 type）与 ``_group_name``（分类名）——
        分组本身不下发 ``id``，拍平后才知道任务属于哪一类。
        """
        return self.flatten(self.task_list())

    def task_route(self, task: dict[str, Any]) -> str:
        """判断任务该走哪条完成路径（纯依据响应字段，不做臆测）。"""
        if self._truthy(task.get('signTask')) or task.get('taskCatg') == 'sign':
            return self.ROUTE_SIGN
        if str(task.get('type')) == '9':
            return self.ROUTE_AD
        if self._truthy(task.get('webTask')) and task.get('jumpURL'):
            return self.ROUTE_WEB
        return self.ROUTE_WISH

    def sign_biz_for_task(self, task: dict[str, Any]) -> str | None:
        """签到类任务 → 该签哪个业务。

        服务端下发 ``name`` 形如「游戏中心积分签到任务」，`:meth:`sign_biz_list``
        的 ``bizName`` 是「游戏中心」；按名字包含关系匹配，匹配不到则返回 ``None``。
        注：``sysCode`` 与 ``vvc_pn`` 包名一一对应（见 :meth:`signable_biz_list`），
        匹配不到时**不猜**，直接报无法完成。
        """
        name = str(task.get('name') or '')
        for biz, pkg, biz_name in self.signable_biz_list():
            if biz_name and biz_name in name:
                return biz
        return None

    def complete_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """按 :meth:`task_route` 执行单个任务的完成上报（写操作）。"""
        route = self.task_route(task)
        if route == self.ROUTE_SIGN:
            biz = self.sign_biz_for_task(task)
            if not biz:
                return {'route': route, 'ok': False, 'message': f"『{task.get('name')}』匹配不到 signBizList 里的业务，未猜包名"}
            res = self.sign_do(biz, pkg=self.pkg_for(biz))
            return {'route': route, 'biz': biz, 'ok': self.is_signed_code(res), 'response': res}
        if route == self.ROUTE_WEB:
            return {'route': route, 'ok': True, 'response': self.complete_web_task(task.get('keyId'))}
        if route == self.ROUTE_AD:
            app = task.get('app') or {}
            if not (app.get('appId') and task.get('traceId')):
                return {'route': route, 'ok': False, 'message': '广告任务缺少 appId/traceId，无真实广告参数可上报'}
            return {'route': route, 'ok': True, 'response': self.cpd_report({'eventBizType': 6, 'eventBizId': task.get('id'), 'appId': app.get('appId'), 'traceId': task.get('traceId'), 'reportType': 2})}
        return {'route': route, 'ok': True, 'response': self.task_wish(task.get('id'))}

    def complete_all_tasks(self, *, sign: bool=True, collect: bool=True) -> dict[str, Any]:
        """**一键搞定全部任务**：逐个完成 + 领取待领积分，纯协议。

        :param sign: 签到类任务是否真的去签到（否则只做上报）。
        :param collect: 完成后是否领取 ``unreceviedPoints`` 待领积分。
        :return: ``{"total", "done", "already", "skipped", "failed", "points", "results"}``
        """
        tasks = self.task_center_list()
        out: dict[str, Any] = {'total': len(tasks), 'done': 0, 'already': 0, 'skipped': 0, 'failed': 0, 'points': 0, 'results': []}
        for t in tasks:
            entry: dict[str, Any] = {'id': t.get('id'), 'name': t.get('name'), 'group': t.get('_group_name'), 'type': t.get('type')}
            try:
                if self._truthy(t.get('taskComplete')):
                    entry.update(action='already', route='none')
                    out['already'] += 1
                elif not sign and self.task_route(t) == self.ROUTE_SIGN:
                    entry.update(action='skipped', route=self.ROUTE_SIGN, message='sign=False，跳过签到类任务')
                    out['skipped'] += 1
                else:
                    r = self.complete_task(t)
                    entry['route'] = r.get('route')
                    entry['response'] = r.get('response')
                    if r.get('biz'):
                        entry['biz'] = r['biz']
                    entry['action'] = 'done' if r.get('ok') else 'failed'
                    entry['message'] = r.get('message', '')
                    out['done' if r.get('ok') else 'failed'] += 1
                if collect:
                    pts = t.get('unreceviedPoints')
                    if pts:
                        cr = self.ec_receive_point(t)
                        entry['collect'] = cr
                        if isinstance(cr, dict) and cr.get('retcode') == 0:
                            got = (cr.get('data') or {}).get('rewardPoints') or 0
                            out['points'] += int(got or 0)
            except Exception as exc:
                entry.update(action='failed', message=f'{type(exc).__name__}: {exc}')
                out['failed'] += 1
            out['results'].append(entry)
        return out
    PLAY_METHODS = {0: '秒开游戏', 1: '下载游戏', 2: '预约游戏'}
    PLAY_METHOD_REWARD_KEY = {0: 'openReward', 1: 'downloadReward', 2: 'appointmentReward'}
    GAME_CAPTCHA_SCENE = 'gameAct'
    GAME_NO_ACTIVITY = 12706

    def game_list(self, play_method: int=2) -> dict[str, Any]:
        """``POST /api/gameact/gameList.do`` —— **游戏活动列表**。

        :param play_method: 0 秒开 / 1 下载 / 2 预约（见 :data:`PLAY_METHODS`）。

        ⚠️ **必须用 POST**：实测 GET 一律返回 ``1000 服务器内部异常``，
        POST 才返回真实结果。源码里写的是 ``method:"get"``，但该网关实际只读请求体。

        无活动时返回 ``retcode=12706 无可用活动``（不是错误，是当前确实没有活动）。
        有活动时 ``data`` 含 ``games[]``、``login``、``totalPoints``、``playMethodInfos[]``。
        """
        return self._call(self.pointh5, '/api/gameact/gameList.do', method='POST', params={'playMethod': int(play_method)})

    def game_rule_desc(self) -> dict[str, Any]:
        """``POST /api/gameact/ruleDesc.do`` —— 游戏任务规则文案（实测 ``retcode:0``）。"""
        return self._call(self.pointh5, '/api/gameact/ruleDesc.do', method='POST', params={})

    def game_reward_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/gameact/getRewardStatus.do`` —— 查询某游戏各玩法的领取状态。"""
        return self._call(self.pointh5, '/api/gameact/getRewardStatus.do', method='POST', params=params or {})

    def game_objective(self, game: dict[str, Any], play_method: int=2) -> Any:
        """取该玩法对应的 ``objective``（源码里它是服务端下发的任务目标值）。"""
        key = self.PLAY_METHOD_REWARD_KEY.get(int(play_method))
        reward = game.get(key) if key else None
        return reward.get('objective') if isinstance(reward, dict) else None

    def game_upload(self, game: dict[str, Any], *, stat: int=4, play_method: int=2) -> dict[str, Any]:
        """``POST /api/gameact/upload.do`` —— **游戏玩法上报（写操作）**。

        源码 ``getGameUpload``::

            {noLoading:true, pkgName:t.pkgName, stat:4, cookie:app.imei,
             token:t.token, playMethod:2}

        ``cookie`` 就是本机设备号（:mod:`client.device`）—— 与签到用的是同一个值。
        上报成功后客户端会跳 ``vivogame://game.vivo.com/openjump?...`` 去游戏中心完成动作。
        """
        return self._call(self.pointh5, '/api/gameact/upload.do', method='POST', params={'noLoading': True, 'pkgName': game.get('pkgName'), 'stat': stat, 'cookie': self.device.imei, 'token': game.get('token'), 'playMethod': int(play_method)})

    def game_reward(self, game: dict[str, Any], *, play_method: int=2, objective: Any=None, scene: str | None=None, ticket: str | None=None, const_id: str | None=None) -> dict[str, Any]:
        """``POST /api/gameact/getReward.do`` —— **领取游戏任务积分（写操作）**。

        源码三个组件（秒开 / 下载 / 预约）的 ``getGameReward`` 参数一致，只有
        ``objective`` 的来源不同::

            {noLoading:true, gameCfgId:t.id, token:t.token, pkgName:t.pkgName,
             objective:t.<open|download|appointment>Reward.objective,
             cookie:app.imei, scene, ticket, constId}

        响应 ``retcode``：

        * ``0``    —— 领到积分，``data.rewardPoints`` 是本次数量；
        * ``1050`` —— 需过验证码，源码转到 ``getCaptcha({scene:"gameAct"})``
          再带 ``ticket``/``constId`` 重发（即 :meth:`game_reward` 带上 ``scene`` 后的形态）；
        * ``1072`` —— 儿童账号禁止使用。
        """
        params: dict[str, Any] = {'noLoading': True, 'gameCfgId': game.get('id'), 'token': game.get('token'), 'pkgName': game.get('pkgName'), 'objective': objective if objective is not None else self.game_objective(game, play_method), 'cookie': self.device.imei}
        if scene:
            params['scene'] = scene
        if ticket:
            params['ticket'] = ticket
        if const_id:
            params['constId'] = const_id
        return self._call(self.pointh5, '/api/gameact/getReward.do', method='POST', params=params)

    def game_tasks(self, *, play_methods: tuple[int, ...]=(0, 1, 2)) -> dict[str, Any]:
        """**全自动跑一遍游戏任务**：三种玩法各拉一次列表，把能领的都领掉。

        只有 ``<x>Reward.status == 1``（已完成、待领取）才调 :meth:`game_reward`；
        ``status == 2`` 是已领完，跳过。服务端返回 ``12706`` 表示当前无活动。

        :return: ``{"methods": [...], "rewarded": n, "points": n, "results": [...]}``
        """
        out: dict[str, Any] = {'methods': [], 'rewarded': 0, 'points': 0, 'results': []}
        for pm in play_methods:
            name = self.PLAY_METHODS.get(pm, str(pm))
            entry: dict[str, Any] = {'playMethod': pm, 'name': name}
            try:
                res = self.game_list(pm)
                entry['retcode'] = res.get('retcode')
                entry['message'] = res.get('message')
                data = res.get('data') or {}
                games = data.get('games') or []
                entry['games'] = len(games)
                out['methods'].append(entry)
                if not games:
                    if res.get('retcode') == self.GAME_NO_ACTIVITY:
                        entry['state'] = '无可用活动'
                    continue
                key = self.PLAY_METHOD_REWARD_KEY.get(pm)
                for g in games:
                    reward = g.get(key) if key else None
                    r: dict[str, Any] = {'game': g.get('name'), 'id': g.get('id'), 'playMethod': pm}
                    status = (reward or {}).get('status')
                    r['status'] = status
                    if status == 1:
                        rr = self.game_reward(g, play_method=pm)
                        r['retcode'] = rr.get('retcode')
                        r['message'] = rr.get('message')
                        r['response'] = rr
                        if rr.get('retcode') == 0:
                            got = int((rr.get('data') or {}).get('rewardPoints') or 0)
                            out['rewarded'] += 1
                            out['points'] += got
                            r['action'] = 'rewarded'
                            r['points'] = got
                        elif rr.get('retcode') == SIGN_NEED_DEVICE:
                            r['action'] = 'need_captcha'
                            r['hint'] = f'需过风控验证码：scene={self.GAME_CAPTCHA_SCENE}，带 ticket/constId 重发 game_reward()'
                        else:
                            r['action'] = 'failed'
                    else:
                        r['action'] = 'skipped'
                    out['results'].append(r)
            except Exception as exc:
                entry['error'] = f'{type(exc).__name__}: {exc}'
                out['methods'].append(entry)
        return out

    def sign_biz_list(self) -> dict[str, Any]:
        """``GET /api/mall/task/center/new/signBizList.do`` —— **App 侧任务列表**。

        源码调用点：``newHome.getSignBizList({noToast: true})``。

        返回 ``data`` 数组，每项：``bizName, bizCode, bizLogo, packageName,
        selfJumpUrl, thirdJumpUrl, showOrder, isSign``。
        """
        return self._call(self.pointh5, '/api/mall/task/center/new/signBizList.do', params={'noToast': 'true'})

    def sign_confirm(self, params: dict[str, Any] | None=None) -> dict[str, Any]:
        """``GET /api/mall/task/center/sign/confirm.do`` —— 签到确认。"""
        return self._call(self.pointh5, '/api/mall/task/center/sign/confirm.do', params=params or {})

    def task_center_float(self) -> dict[str, Any]:
        """``GET /api/mall/task/center/float.do`` —— 浮动入口配置。"""
        return self._call(self.pointh5, '/api/mall/task/center/float.do')

    def point_recent_stat(self) -> dict[str, Any]:
        """``GET /api/user/point/listRecent3MonthStat.do``。"""
        return self._call(self.pointh5, '/api/user/point/listRecent3MonthStat.do')

    def point_detail_for_month(self, month: str | None=None, page: int=1, detail_type: int=0) -> dict[str, Any]:
        """``GET /api/user/point/listDetailForMonth.do`` —— 月度积分明细。

        源码调用点（chunk-2 / chunk-21）::

            listDetailForMonth({month: e, page: t, type: this.cellType, noLoading: true})

        :param month: ``YYYY-MM``（**实测：必须是这个格式**，``YYYYMM`` 会 `1001 请求参数错误`）
        :param detail_type: `0`=全部、`1`=获取、`2`=消耗（源码 tab_des: 0 全部 / 1 获取 / 2 消耗）
        """
        import time as _t
        p = {'month': month or _t.strftime('%Y-%m'), 'page': page, 'type': detail_type, 'noLoading': True}
        return self._call(self.pointh5, '/api/user/point/listDetailForMonth.do', params=p)

    def cash_point_balance(self) -> dict[str, Any]:
        """``GET /api/user/cash/point/balance.do`` —— 可抵现积分余额。"""
        return self._call(self.pointh5, '/api/user/cash/point/balance.do')

    def member_level(self) -> dict[str, Any]:
        """``GET /api/member/level/query.do`` —— 会员等级（含升级跳转）。"""
        return self._call(self.pointh5, '/api/member/level/query.do')

    def task_wish(self, task_id: Any, *, extra: dict[str, Any] | None=None) -> dict[str, Any]:
        """``POST //pointsdk.vivo.com.cn/web/task/wish``（源码 ``taskWish``）—— **实测可用**。

        源码参数：``{noLoading:true, noToast:true, taskId}``。

        ⚠️ **必须带真实 vivo 包名**（默认 ``com.vivo.game``）：
        不带 ``vvc_pn`` 或带 ``miniprogram`` 都会返回 ``10001 当前客户端不支持``。
        """
        p = {'taskId': task_id, 'noLoading': True, 'noToast': True}
        if extra:
            p.update(extra)
        return self._call(self.pointsdk, '/web/task/wish', method='POST', pkg=self.cfg.get('default_package') or DEFAULT_PACKAGE, params=p)

    def wish_list(self, params: dict[str, Any] | None=None) -> dict[str, Any]:
        """``GET /api/wish/list.do`` —— 心愿单列表。"""
        return self._call(self.pointh5, '/api/wish/list.do', params=params or {})

    def wish_add(self, params: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/wish/add.do`` —— 添加心愿（写操作）。"""
        return self._call(self.pointh5, '/api/wish/add.do', method='POST', params=params)

    def wish_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/wish/delete.do`` —— 删除心愿（写操作）。"""
        return self._call(self.pointh5, '/api/wish/delete.do', method='POST', params=params)

    def cpd_report(self, params: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/mall/cpd/task/app/report.do`` —— 广告任务上报。

        源码参数：``{eventBizType:6, eventBizId, appId, traceId, reportType:2}``。
        源码里若存在 ``window.MaodunCipher`` 会把整个 params 加密成 ``{jvq_param}``，
        响应再用 ``MaodunCipher.decrypt`` 还原（本客户端未实现该加解密）。
        """
        return self._call(self.pointh5, '/api/mall/cpd/task/app/report.do', method='POST', params=params)

    def cpd_receive(self, params: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/mall/cpd/task/app/receive.do`` —— 广告任务领奖（写操作）。

        源码参数：``{eventBizType:6, eventBizId, appId, rewardPhase:2, traceId}``；
        ``retcode 1028`` = 该应用奖励已被领取。
        """
        return self._call(self.pointh5, '/api/mall/cpd/task/app/receive.do', method='POST', params=params)

    def cpc_signature(self, url: str) -> dict[str, Any]:
        """``POST /api/mall/cpc/generate/monitor/signature.do`` —— 计算监控签名。

        返回 ``data`` 为签名串，源码把它拼成 ``url + "&s=" + data`` 再上报。
        """
        return self._call(self.pointh5, '/api/mall/cpc/generate/monitor/signature.do', method='POST', params={'url': url})

    def cpc_list(self, params: dict[str, Any] | None=None) -> dict[str, Any]:
        """``GET /api/mall/cpc/list.do``"""
        return self._call(self.pointh5, '/api/mall/cpc/list.do', params=params or {})

    def cpc_feed_ads(self, params: dict[str, Any] | None=None) -> dict[str, Any]:
        """``GET /api/mall/cpc/feedAds/list.do``"""
        return self._call(self.pointh5, '/api/mall/cpc/feedAds/list.do', params=params or {})

    def cpc_monitor_report(self, params: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/mall/cpc/monitor/report.do``"""
        return self._call(self.pointh5, '/api/mall/cpc/monitor/report.do', method='POST', params=params)

    def offline_store_points(self, params: dict[str, Any] | None=None) -> dict[str, Any]:
        """``GET /offline/api/store/listPoints.do`` —— 门店积分。

        实测需**导购身份**：``12211 导购端用户认证失败``。
        """
        return self._call(self.pointh5, '/offline/api/store/listPoints.do', params=params or {})

    def sign_info(self, biz: str='vivop') -> dict[str, Any]:
        """``GET /api/sign/info.do`` —— **按业务查签到活动**。

        ``biz`` 取 App 的 ``bizCode``（见 :meth:`sign_biz_list`）或直接给包名：
        ``vivop``（vivo+，活动 id=71，**不带 ``vvc_pn``**）、
        ``appstore`` / ``gamecenter`` / ``wallet`` / ``iMusic`` …

        实测关键：``vvc_pn`` **要么不带（vivo+），要么必须是真实 vivo 包名**；
        带 ``miniprogram`` 会返回 ``10001 当前客户端不支持签到``。
        """
        return self._call(self.sign_host, '/api/sign/info.do', pkg=self.pkg_for(biz), params={'sysCode': biz, 'fromMini': '108', 'source': 'vivoplus'}, referer=H5_REFERER)

    def sign_activity_id(self, biz: str='vivop') -> Any:
        """便捷：直接返回该业务签到活动的 ``id``。"""
        d = self.sign_info(biz).get('data')
        return d.get('id') if isinstance(d, dict) else None

    def sign_do(self, biz: str='vivop', extra: dict[str, Any] | None=None, pkg: str | None='_auto_') -> dict[str, Any]:
        """``POST /api/sign/dosign.do`` —— **执行签到（写操作）**。

        源码里 H5 只发 ``{sysCode}``。成功时 ``code:0``；当天已签到返回
        ``10003 不要重复签到``。

        .. note::
           此前文档把 ``1050`` 记作"必须人工过顶象验证码"。**实测更正**：
           ``1050`` 是**设备标识缺失**触发的服务端降级风控（转而要求人机验证）。
           带上 ``vvc_imei`` 后该分支不再出现，签到纯协议可达。
           见 :mod:`client.device`。

        实测矩阵（``vvc_pn`` = 真实包名）：

        | Cookie | 结果 |
        |---|---|
        | （无） | `1010 用户未登录` |
        | `vvc_openid`+`vvc_r` | `1010 用户未登录` |
        | + `vvc_pn=miniprogram` | `10001 当前客户端不支持签到` |
        | + `vvc_pn=<真实包名>`，无设备标识 | `1050 账户异常` ← 设备校验未过 |
        | + **`vvc_imei=<任意非空>`** | **``code:0`` 签到成功** |
        | + `vvc_oaid=<任意非空>` | **``code:0`` 签到成功** |
        """
        params: dict[str, Any] = {'sysCode': biz}
        if extra:
            params.update(extra)
        use_pkg = self.pkg_for(biz) if pkg == '_auto_' else pkg
        return self._call(self.sign_host, '/api/sign/dosign.do', method='POST', pkg=use_pkg, params=params, referer=H5_REFERER)

    @staticmethod
    def is_signed_code(res: Any) -> bool:
        """``dosign`` 的响应是否表示"签到已达成"（含当天已签过的 ``10003``）。"""
        if not isinstance(res, dict):
            return False
        try:
            code = int(res.get('code', -1))
        except (TypeError, ValueError):
            return False
        return code in (0, SIGN_ALREADY_DONE)

    def signable_biz_list(self) -> list[tuple[str, str, str]]:
        """可签到的业务列表，``[(bizCode, packageName, bizName), ...]``。

        取自 :meth:`sign_biz_list`（服务端下发，非写死）。
        只保留有 ``packageName`` 的项 —— 签到写接口需要真实包名才能过客户端校验。
        """
        try:
            rows = self.sign_biz_list().get('data') or []
        except Exception:
            rows = []
        out: list[tuple[str, str, str]] = []
        for b in rows:
            biz, pkg = (b.get('bizCode'), b.get('packageName'))
            if biz and pkg:
                out.append((str(biz), str(pkg), str(b.get('bizName') or biz)))
        if not out:
            out = [(k, v, k) for k, v in BIZ_PACKAGE.items() if v]
        return out

    def sign_all(self, *, skip_signed: bool=True) -> list[dict[str, Any]]:
        """**逐个业务执行签到**（全自动，无需人工）。

        对每个业务先查 ``sign/info.do`` 的 ``todayIsSign``，已签到则跳过，
        否则调 ``sign/dosign.do``。返回每个业务的结果明细。

        :param skip_signed: 已签到的业务是否跳过（默认跳过，避免无谓请求）。
        """
        results: list[dict[str, Any]] = []
        for biz, pkg, name in self.signable_biz_list():
            entry: dict[str, Any] = {'biz': biz, 'name': name, 'package': pkg}
            try:
                if skip_signed:
                    info = self.sign_info(biz)
                    d = info.get('data') if isinstance(info, dict) else None
                    if isinstance(d, dict) and self._truthy(d.get('todayIsSign')):
                        entry.update(action='skipped', reason='今日已签到', activity_id=d.get('id'))
                        results.append(entry)
                        continue
                res = self.sign_do(biz, pkg=pkg)
                code = res.get('code') if isinstance(res, dict) else None
                msg = res.get('msg') or res.get('message') if isinstance(res, dict) else ''
                entry.update(code=code, message=msg, response=res)
                if code == SIGN_DEVICE_MISMATCH:
                    entry['hint'] = f'设备号 {self.device.imei} 与本账号不匹配。该账号此前可能用过别的设备号（多账号共用或中途改过）——详见 client/device.py。'
                if self.is_signed_code(res):
                    entry['action'] = 'already' if code == SIGN_ALREADY_DONE else 'signed'
                else:
                    entry['action'] = 'failed'
                results.append(entry)
            except Exception as exc:
                entry.update(action='error', message=f'{type(exc).__name__}: {exc}')
                results.append(entry)
        return results

    @staticmethod
    def _truthy(v: Any) -> bool:
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        s = str(v)
        if s.isdigit():
            return int(s) != 0
        return s.lower() in ('true', 'yes', '1', 'y')

    def sign_notify(self, notify_flag: int=1, biz: str='vivop') -> dict[str, Any]:
        """``POST sign.vivo.com.cn/api/sign/notify/notify.do`` —— **签到提醒开关（写操作，实测可用）**。

        源码（`mod-1b33` 的 `toggleSwitch`）::

            changeNotifyStatus({notifyFlag: +this.remindStatus})

        实测：``notifyFlag=1`` / ``0`` 均返回 ``{"code":0}``。
        """
        return self._call(self.sign_host, '/api/sign/notify/notify.do', method='POST', pkg=self.cfg.get('default_package') or DEFAULT_PACKAGE, params={'notifyFlag': int(notify_flag), 'sysCode': biz}, referer=H5_REFERER)

    def sign_point_info(self, biz: str='vivop') -> dict[str, Any]:
        """``GET /api/sign/capital/pointInfo.do`` —— 签到侧积分/等级（需 vivo 账号 cookie）。"""
        return self._call(self.sign_host, '/api/sign/capital/pointInfo.do', pkg=self.pkg_for(biz), with_vivo_account=True, params={'sysCode': biz}, referer=H5_REFERER)

    def consecutive_receive(self, biz: str='vivop', params: dict[str, Any] | None=None) -> dict[str, Any]:
        """``POST /api/sign/consecutive/receive.do`` —— 连续签到领奖（写操作）。"""
        return self._call(self.sign_host, '/api/sign/consecutive/receive.do', method='POST', pkg=self.pkg_for(biz), with_vivo_account=True, params=params or {}, referer=H5_REFERER)

    def sign_home(self) -> dict[str, Any]:
        """``GET pointh5/api/sign/home.do`` —— 积分 SPA 的签到首页（**实测可用**）。

        返回 ``data``：``{taskId, taskType, totalDays, signDays[], hasSigned,
        dailyReward, todaySignReward, nextSignReward, allowPush, totalPoints,
        bonus[{day,point,imageUrl,acquiredImageUrl}]}``。

        实测：``taskId=183``、``hasSigned=false``、``dailyReward=1``，
        ``bonus`` = 3 天→5 分 / 7 天→7 分 / 10 天→9 分。
        """
        return self._call(self.pointh5, '/api/sign/home.do')
    MAIN_SIGN_PACKAGES = ('com.vivo.wallet',)

    def sign_main(self, packages: tuple[str, ...] | None=None) -> dict[str, Any]:
        """**vivo+ SPA 侧主签到**（``pointh5``），全自动、零人工。

        ## 实测语义（2026-10-07，两个账号复现）

        * ``/api/sign/home.do`` 与 ``/api/sign/start.do`` 都用 ``vvc_pn`` 选**客户端身份**；
        * **``taskId`` 被服务端忽略** —— 传 176/183/418/544 结果完全一样；
          真正决定签哪个活动的是 ``vvc_pn``（实测它只签该客户端自己的活动）；
        * **只有 ``com.vivo.wallet`` 能过风控**：

          | ``vvc_pn`` | ``start.do`` |
          |---|---|
          | `com.vivo.wallet` | ✅ `retcode:0` 签到成功 / `10002 今日已签到` |
          | `com.bbk.appstore` / `com.vivo.game` / `com.vivo.browser` / `com.bbk.theme` | ❌ `1050` |
          | `miniprogram` | ❌ `1001 签到任务不存在` |
          | 不带 | ❌ `1010 用户未登录` |

        * 链路：``GET sign/home.do``（wallet 身份）取 ``hasSigned`` → 未签则
          ``POST sign/start.do``；``0`` 成功、``10002`` 今日已签到（幂等达成）。

        :return: ``{"action", "signed", "skipped", "failed", "total", "results"}``
        """
        pkgs = packages or self.MAIN_SIGN_PACKAGES
        results: list[dict[str, Any]] = []
        signed = skipped = failed = 0
        for pkg in pkgs:
            home = self._call(self.pointh5, '/api/sign/home.do', pkg=pkg)
            data = (home or {}).get('data') or {}
            entry: dict[str, Any] = {'via': 'pointh5.vivo.com.cn/api/sign/start.do', 'package': pkg, 'task_id': data.get('taskId')}
            if self._truthy(data.get('hasSigned')):
                entry.update(action='skipped', reason='今日已签到', total_days=data.get('totalDays'), total_points=data.get('totalPoints'))
                skipped += 1
                results.append(entry)
                continue
            res = self._call(self.pointh5, '/api/sign/start.do', method='POST', pkg=pkg, params={'taskId': data.get('taskId')})
            code = res.get('code', res.get('retcode')) if isinstance(res, dict) else None
            msg = res.get('msg') or res.get('message') if isinstance(res, dict) else ''
            done = code in (0, 200) or code == SPA_ALREADY_SIGNED
            entry.update(code=code, message=msg, response=res)
            if done:
                d2 = self._call(self.pointh5, '/api/sign/home.do', pkg=pkg).get('data') or {}
                entry.update(total_days=d2.get('totalDays'), total_points=d2.get('totalPoints'), sign_days=d2.get('signDays'))
            entry['action'] = 'already' if code == SPA_ALREADY_SIGNED else 'signed' if done else 'failed'
            if entry['action'] in ('signed', 'already'):
                signed += 1
            else:
                failed += 1
            results.append(entry)
        action = 'signed' if signed else 'skipped' if skipped else 'failed'
        return {'via': 'pointh5.vivo.com.cn/api/sign/start.do', 'action': action, 'signed': signed, 'skipped': skipped, 'failed': failed, 'total': len(results), 'results': results}

    def sign_home_list(self) -> dict[str, Any]:
        """``GET pointh5/api/mall/task/center/signlist.do`` —— **App 签到列表（SPA 版）**。

        实测返回 10 条 ``signInfos``，每条 ``{code, name, logo, signJumpUrl, isSign}``。
        与 :meth:`sign_biz_list` 的区别：这里给的是**原生 App 深链**
        （``hap://`` / ``vivovideo://`` 等），适合在 vivo 设备上直接跳转。
        """
        return self._call(self.pointh5, '/api/mall/task/center/signlist.do')

    def sign_home_point_list(self) -> dict[str, Any]:
        """``GET pointh5/api/mall/task/center/point/signlist.do`` —— 积分商城侧 App 签到列表。"""
        return self._call(self.pointh5, '/api/mall/task/center/point/signlist.do')

    def sign_start(self, *, task_id: Any=None, task_type: str | None=None, extra: dict[str, Any] | None=None) -> dict[str, Any]:
        """``POST pointh5/api/sign/start.do`` —— SPA 侧**执行签到**（写操作）。

        源码（`app.js` 偏移 95562）::

            M = { getSignInfo: "api/sign/home.do?_t=" + Date.now(),
                  signIn:      "api/sign/start.do",
                  pushSwitch:  "api/sign/pushSwitch.do" }

        实测：**与 `sign.vivo.com.cn` 一样返回 `1050`**（同一套风控），
        同样需先过顶象验证码、再把 ``{ticket, scene, constId}`` 带上重发。
        """
        params: dict[str, Any] = {}
        if task_id is not None:
            params['taskId'] = task_id
        if task_type:
            params['taskType'] = task_type
        if extra:
            params.update(extra)
        return self._call(self.pointh5, '/api/sign/start.do', method='POST', params=params)

    def activity_task_list(self) -> dict[str, Any]:
        """``GET pointh5/api/mall/task/center/activitylist.do`` —— 活动任务列表（实测可用）。"""
        return self._call(self.pointh5, '/api/mall/task/center/activitylist.do')

    def unrecv_point(self) -> dict[str, Any]:
        """``GET pointh5/api/task/unrecvPoint.do`` —— 待领积分（实测 ``{sign:false, unrecvPoint:0}``）。"""
        return self._call(self.pointh5, '/api/task/unrecvPoint.do')

    def site_config(self, site_id: str | None=None) -> dict[str, Any]:
        """``GET wukongapi.vivo.com.cn/wukongtopic/site/online/query`` —— 悟空的站点配置。

        页面组件的 ``belongClient`` / ``cherryTaskShow`` / ``source`` 等都在这里，
        是判断任务接口参数的关键依据。
        """
        sid = site_id or self.site['site_id']
        return self._call('https://wukongapi.vivo.com.cn', '/wukongtopic/site/online/query', params={'siteId': sid}, pkg=None, referer=H5_REFERER)

    def flatten(self, res: dict[str, Any]) -> list[dict[str, Any]]:
        """把 ``task_list`` 的响应拍平成 ``[{group, group_name, ...task}]``。"""
        out: list[dict[str, Any]] = []
        data = (res or {}).get('data') or {}
        for grp in data.get('tasks') or []:
            gtype = str(grp.get('type'))
            gname = grp.get('name') or TASK_GROUPS.get(gtype, gtype)
            for t in grp.get('list') or []:
                item = dict(t)
                item['_group'] = gtype
                item['_group_name'] = gname
                out.append(item)
        return out

    def summarize(self, res: Any) -> str:
        if not isinstance(res, dict):
            return str(res)[:200]
        return json.dumps({k: res.get(k) for k in ('code', 'retcode', 'success', 'msg', 'message', 'login') if k in res}, ensure_ascii=False)
