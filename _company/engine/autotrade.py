"""🤖 자동매매 — 토스증권 오픈API로 규칙대로 사고팔기.

모드: off(꺼짐, 기본) · paper(가상: 주문은 장부에만) · live(실전: 토스로 지정가 주문)
시장: US(미장, 기본) · KR(국장)
전략: surge(급등주, 기본) · trend(꾸준한 상승 추세, 국장)
예산: all_cash(계좌의 그 통화 현금 전부, 기본) · fixed(총 투자 한도 금액)

💰 돈은 계좌 밖으로 못 나가요: 토스 API 에 출금·이체 기능이 없고, toss.py 는 '주문 생성·취소'만 허용해요.

급등주 규칙
- 사기: 장중 몇 분마다 picker.surge_scan 이 고른 '오늘 +N% 이상 + 거래량 폭증' 종목 (장 시작 10분 뒤 ~ 마감 30분 전)
  → 바로 안 사고 '반등 확인' 후: 고점에서 눌림 → VWAP·MA5 위 회복 → 직전 1분봉 연속 상승 (rebound_check)
- 팔기: 손절(-3%) · 고점 대비 하락(-4%, 수익 지키기) · 익절(+10%) · 장 마감 15분 전 전부 정리 (밤새 안 들고 감)

안전장치 (코드에서 강제)
- 자동매매가 산 주식만 사고팔고, 사장님이 직접 산 주식·직접 낸 주문은 안 건드림
- 예산(현금 전부 또는 고정 한도)을 고른 종목 수로 나눠 종목당 상한 · 미수/신용 안 씀
- 하루 손실 한도(자동매매 손익 기준)를 넘으면 그날 정지 · 하루 매수 횟수 한도 (매도는 언제나)
- 같은 종목 미체결이 있으면 새 주문 안 함 · 몇 분 안에 안 채워진 주문 자동 취소
- 긴급 정지: 꺼짐 + 자동매매가 낸 미체결 주문 전부 취소
- (선택) 주문 전 AI 검토

설정: _shared/autotrade.json (🤖 자동매매 화면에서 바꿈, 자기 개선 기능은 못 고침)
기록: _cache/autotrade_state.json (git 에 안 올라감)
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import backtest as bt
from . import arena, edge, extras, news, picker, shadow, store, toss
from . import tradelearn as tl
from .agents import COMPANY_DIR, SHARED_DIR

CONFIG = SHARED_DIR / "autotrade.json"
STATE = COMPANY_DIR / "_cache" / "autotrade_state.json"
KST = timezone(timedelta(hours=9))
DEFAULTS = {
    "v": 8,                        # 설정 형식 버전 (예전 기본값 자동 갱신용)
    "mode": "off",
    "markets": ["US", "KR"],       # 동시에 돌릴 시장 (국장 09:00~15:30 · 미장 프리장 포함 저녁~새벽, 시장마다 기록·한도·정지 따로)
    "market": "US",                # (내부용) 지금 처리 중인 시장 — 설정 화면에서는 markets 를 씀
    "strategy": "auto",            # auto = 단타(급등)·스윙(추세) 후보를 '기대 수익률' 순으로 섞어서 / surge / trend
    "auto_pick": True,             # 종목을 규칙이 알아서 고름
    "pick_count": 3,               # 동시에 들고 갈 최대 종목 수 (예산을 이 수로 나눔)
    "symbols": [],                 # 직접 고른 종목 (auto_pick 끄면 이것만)
    "exclude": [],                 # 절대 사지 않을 종목
    "watchlist": [],               # ⭐ 관심 종목: 급등 조건(최소 상승률)이 맞으면 후보로 (다른 관문은 똑같이 거침)
    "budget_mode": "all_cash",     # all_cash = 계좌의 그 통화 현금 전부 / fixed = max_total_krw
    "max_total_krw": 100000,       # fixed 일 때 총 투자 한도
    "max_order_krw": 50000,        # 한 번 주문 최대 (두 방식 모두 상한, 0 = 제한 없음)
    "max_position_krw": 50000,     # 종목당 최대 (두 방식 모두 상한, 0 = 제한 없음)
    "max_orders_per_day": 6,       # 하루 매수 횟수 (매도는 제한 없음)
    "loss_streak_pause": 3,        # 장중 학습: 연속 손실 이만큼이면 잠시 매수 쉬기
    "pause_min": 30,               # 그때 쉬는 시간(분)
    "monthly_target_pct": 10.0,    # 한 달 목표 수익률
    "stop_at_target": True,        # 목표 달성하면 그달은 새로 안 삼 (번 돈 지키기)
    "max_daily_loss_pct": 5.0,     # all_cash: 예산 대비 하루 손실 한도 %
    "max_daily_loss_krw": 5000,    # fixed: 하루 손실 한도 금액
    "stop_loss_pct": 3.0,
    "take_profit_pct": 10.0,       # 남은 물량 전부 익절
    "partial_tp_pct": 4.0,         # 나눠 팔기: 평단 대비 이만큼 오르면 일부 먼저 팔기 (0 = 안 씀)
    "partial_tp_ratio": 0.5,       # 그때 파는 비율 (0.5 = 절반) — 나머지는 손절선을 산 가격으로 올림
    "trail_pct": 4.0,              # 산 뒤 최고가 대비 이만큼 떨어지면 팔기 (최고 수익이 이만큼 이상 났을 때부터 작동)
    "trail_wide_after_pct": 10.0,  # 최고 수익이 이 % 이상이면 고점 대비 기준을 넓힘 (큰 상승은 더 길게)
    "trail_wide_pct": 6.0,         # 그때 쓰는 고점 대비 하락 기준(%)
    "surge_min_change_pct": 5.0,   # 오늘 이만큼 이상 오른 종목
    "surge_max_change_pct": 40.0,  # 너무 많이 오른 건 제외 (국장은 상한가 30%)
    "surge_volume_ratio": 3.0,     # 평소 거래량의 몇 배 이상
    "us_premarket": True,          # 미장 프리장(뉴욕 04:00~)부터 매매
    "pre_volume_ratio": 0.2,       # 프리장: 평소 하루 거래량의 이만큼(20%)이 벌써 거래된 종목
    "min_trading_value_krw": 3000000000,  # 하루 거래대금 최소 (원 환산)
    "us_min_price": 1.0,           # 1달러 미만 동전주 제외
    "limit_slip_pct": 0.5,         # 급등주: 사는 지정가는 현재가 +0.5%, 파는 건 -0.5% (체결 잘 되게)
    "chase_filter": True,          # 🧗 고점 추격 필터 (2호 단타): 오늘 고점 근처인데 직전 5분 되돌림이 없거나 5분 급등이면 안 삼
    "chase_near_high_pct": 1.0,    #   오늘 고점 대비 이 % 이내면 '고점 근처'
    "chase_min_pullback_pct": 0.5, #   직전 5분 고점에서 이 % 이상 눌린 적이 있어야 삼
    "chase_run5_max_pct": 3.0,     #   고점 근처에서 직전 5분 상승률이 이 % 이상이면 안 삼
    "falling5_block": True,        # 📉 2호 단타: 직전 5분 상승률이 마이너스(하락 중)면 안 삼
    "rebound_entry": True,         # 🔄 2호 반등 확인 후 진입: 고점에서 눌린 뒤 VWAP·MA5 위 회복 + 직전 1분봉 연속 상승일 때만 삼
    "rebound_pullback_pct": 1.5,   #   오늘 고점에서 이 % 이상 눌린 적이 있어야 함
    "rebound_pullback_max_pct": 10.0,  #   이 % 넘게 무너졌으면 반등 아님 (안 삼)
    "rebound_up_bars": 2,          #   직전 1분봉 몇 개가 연속 상승이어야 하는지 (1~3)
    "rsi_max_buy": 68,             # 추세(스윙): 과열 기준
    "swing_stop_pct": 6.0,         # 스윙 손절 (며칠 들고 가니 단타보다 넓게)
    "swing_trail_pct": 8.0,        # 스윙: 최고 수익이 이만큼 난 뒤 고점 대비 이만큼 빠지면 팔기
    "swing_trail_vol": False,      # 스윙 고점 대비 기준을 종목 변동성으로 자동 계산 (3~8% 밖이면 swing_trail_pct · 그림자로 비교)
    "swing_trail_vol_mult": 0.7,   #   하루 변동성(최근 20일) × 이 값 = 기준(%) (연 88%→약 3.9%, 182%→약 8%)
    "swing_max_days": 15,          # 스윙 최대 보유 일수
    "interval_min": 1,             # 급등 종목 찾기 간격 (보유 종목 손절 확인은 따로 20초마다)
    "exit_check_sec": 5,
    "ai_check": False,             # 주문 전 AI 검토 (급등주는 속도가 중요해서 기본 꺼짐)
    "auto_tune": True,             # 🔁 매일 장 끝나고 자동 조절 (아래 항목별로 켜고 끔, 모두 범위 안에서만)
    "auto_rules": True,            #   학습 제안(거래량 기준·나눠 팔기 % 등) 자동 적용
    "auto_stop": True,             #   손절 % 자동 (stop_min ~ stop_max)
    "auto_limits": True,           #   종목당·주문 한도 자동 (처음 값의 limit_min_mult ~ limit_max_mult 배)
    "auto_loss": True,             #   하루 손실 한도 자동 (loss_pct_min ~ loss_pct_max %)
    "auto_mode": True,             #   실전↔가상 자동 (끄기·긴급 정지는 절대 자동으로 안 풀림)
    "stop_min": 2.0, "stop_max": 6.0,
    "limit_min_mult": 0.5, "limit_max_mult": 3.0,
    "loss_pct_min": 2.0, "loss_pct_max": 8.0,
    "mode_down_days": 3,           # 실전에서 이 날수 연속 손실이면 가상으로
    "mode_down_month_pct": 8.0,    # 또는 이번 달 이 % 이상 손실이면 가상으로
    "mode_up_trades": 10,          # 가상에서 이 건수 이상·승률 50%↑·평균 + 면 실전 복귀
    "arena_replay": True,          # 📼 AI 리그 과거 장 복습: 장 닫힌 뒤 지난 30일 후보를 그 시점 차트로 다시 판단 → 실제 결과로 채점 (공부용)
    "arena_replay_per_day": 60,    #    하루 복습 후보 수 (후보 하나에 AI 1번 · 토요일은 3배)
    "arena_replay_market": True,   #    🌐 우리가 고른 후보가 모자라면 시장 전체(거래 많은 150종목)의 과거 급등일로 채움
    "swing_auto_off": True,        # 📉 1호 스윙(가상)이 30일·30건↑ 평균 마이너스면 자동으로 쉬게
    "arena_on": True,              # 🏟️ AI 리그: 성격 다른 AI 들이 가상으로만 매매 → 매주 꼴찌 탈락·1등 변형 투입
    "arena_size": 10, "arena_ai_per_day": 0, "arena_min_trades": 20,   #    참가자 수 · 하루 AI 판단 횟수 · 탈락 판정 최소 거래
    "arena_promote": True,         #    🏆 1등 전략을 2호 급등주 AI 판단에 참고로 붙임
    "arena_live": True,            #    💰 상위 arena_live_n 명(검증 통과)이 '산다'고 하면 실전 매수 (실전 모드일 때만 · 채팅으로 켜기 불가)
    "arena_live_n": 2, "arena_live_pct": 30,   #    실전 자격 인원 · 실전 예산 중 AI 리그 몫(%)
    "arena_ctrl_margin": 0.3,      #    🎲 무작위 대조군보다 최근 평균이 이만큼(%p) 이상 나아야 실전 자격
    "arena_half_life_days": 14,    #    ⏳ 성적 계산 때 이 일수가 지난 거래는 절반 무게 (최근 성적을 더 크게)
    "arena_voice": True,           #    🗣️ 리그 AI 가 의견이 생기면 바로 채팅으로 (적용은 사장님이)
    "arena_voice_every": 5,        #       지난번 뒤로 거래가 이만큼 더 쌓이면 물어봄 (3연속 손실이면 바로)
    "arena_voice_per_day": 8,      #       하루 전체 최대 의견 수 (AI 한 명은 하루 1번)
    "daily_digest": True,          # 📋 장 마감 뒤 제안 현황(대기·적용·바꾼 뒤 성적)을 텔레그램으로 하루 한 번
    "notes_council": True,         # 📒 주말 공부 끝에 AI 셋이 회의해서 공유 노트 정리 (다수 동의한 것만 핵심 노트 · 원본 보관)
    "shadow_on": True,             # 🧪 그림자 가상 매매: 규칙 조합 여러 개를 실전과 동시에 가상으로
    "shadow_count": 30,            #   1000개는 운 막는 문턱(promote_edge)이 너무 높아 교체가 0번이었음 ·   동시에 돌릴 일반 조합 수 (한 바퀴 시간 제한 안에서 돌아가며 처리)
    "auto_promote": True,          #   확실히 나은 조합을 실전 규칙으로 자동 교체 (매일 복기 뒤)
    "promote_min_trades": 15,      #   교체하려면 그 조합 거래가 최소 이만큼 (20 보다 작아도 20건 적용 · shadow.PROMOTE_N)
    "promote_margin_pct": 1.0,     #   (예전 기준·지금은 안 씀) 교체는 앞 70% 선정 → 뒤 30% 검증 방식
    "promote_window_days": 14,     #   화면 표시용 최근 기간(일)
    "backtest_on": True,           # 📼 매일 장 끝나고 백테스트 → 상위 조합을 그림자 매매로
    "ai_take_profit": "test",      # 🤖 익절을 AI 에게: off=규칙대로 · test=규칙대로 팔되 'AI 였으면?'을 따로 비교 · on=익절 규칙 대신 AI 가 판단
    "ai_tp_guard_peak": 3,         #    on 일 때 안전장치: 최고 수익이 +이 % 이상이었다가
    "ai_tp_guard_floor": 0.3,      #    +이 % 까지 밀리면 무조건 팔기 (손절·마감 정리·하루 손실 한도는 그대로 먼저)
    "ai_tp_min_trades": 10,        #    test 비교가 이 건수 이상 쌓이고
    "ai_tp_margin_pct": 0.3,       #    AI 쪽이 평균 이 %p 이상 나으면 자동으로 on
    "ai_decide": True,             # 🤖 거래마다 AI 판단: 사기 전 살지·얼마나, 보유 중 일찍 팔지 (손절·마감 정리·한도는 못 바꿈)
    "ai_max_per_hour": 30,         #   AI 판단 시간당 최대 횟수 (넘으면 규칙대로)
    "ai_hold_check_min": 5,        #   보유 종목을 AI 가 다시 보는 간격(분)
    "prob_sizing": True,           # 학습 기록으로 승률을 추정해 살 금액 조절 (25%~100%, 한도는 절대 안 넘음)
    "min_size_ratio": 0.25,        # 승률이 낮을 때 최소로 사는 비율
    "min_win_prob": 0,             # 추정 승률이 이보다 낮으면 안 삼 (0 = 안 씀)
    "no_rebuy_after_loss": True,   # 🔁 손해 보고 판 종목은 다시 안 삼: 단타는 그날, 스윙은 아래 일수 동안 (손절 후 재진입 반복 방지)
    "no_rebuy_swing_days": 3,
    "explore_per_day": 2,          # 🔬 기대 수익이 마이너스지만 기록이 적어 확신 못 하는 후보: 하루 이만큼만 최소 금액으로 '탐색 매수' (0 = 안 삼) · 2호 가상 매수엔 한도 없음
    "lane_swing_mode": "live",     # 🤝 1호 일반(스윙) AI — live: 실전 모드 따라감 · paper: 가상 장부 · off: 새로 안 삼 (보유는 계속 관리)
    "lane_surge_mode": "paper",    # 🤝 2호 급등주(단타) AI — 처음엔 가상으로 돌리고 성적 확인되면 자동으로 실전 (lane_auto_live)
    "lane_share_surge": 50,        #    같은 장부를 쓰는 두 AI 의 예산 나누기: 2호 몫(%) (나머지 1호) — 자동 조정
    "lane_auto_share": True, "lane_share_min": 30, "lane_share_max": 70,   #    잘하는 쪽으로 하루 5%p 씩, 이 범위 안에서
    "lane_auto_live": True,        #    2호(가상)가 아래 조건을 채우면 실전으로 · 실전 쪽이 연속 손실이면 가상으로
    "lane_live_trades": 30, "lane_live_days": 14,   #    최근 N일 끝난 거래 M건 이상 · 평균 플러스 · 승률 45%↑
    "lane_cross_review": True,     #    장 마감 후 두 AI 가 서로 거래를 복기 → 공유 노트(교훈)에 기록
    "regime_on": True,             # 📉 시장 분위기: 지수(QQQ·KODEX200)가 크게 빠지는 날 급등주 덜 사기
    "regime_half_pct": 1.5,        #    지수 오늘 -이 % 이하면 급등주 금액 절반
    "regime_stop_pct": 3,          #    -이 % 이하면 급등주 새로 안 삼
    "tod_filter": True,            # 🕐 최근 30일 계속 지는 시간대(10건↑·승률 35%↓·평균 마이너스)엔 급등주 안 삼
    "max_per_sector": 3,           # 🧺 같은 업종 동시 보유 최대 (0 = 안 봄 · 토스가 업종을 알려줄 때만)
    "backup_on": True, "backup_dir": "",   # 💾 하루 한 번 학습·계좌 기록 백업 (비우면 문서\자동매매_백업)
    "weekend_study": True,         # 📚 토요일 오전: 긴 백테스트·과거 급등 패턴·놓친 종목·그림자 정리·주간 AI 복기
    "weekly_report": True, "tomorrow_plan": True,   # 📊 주간 성적표 · 🗓️ 장 마감 뒤 내일 계획 (텔레그램)
    "improve_effect_check": True,  # 🛠️ 매매 코드 자동 개선 뒤 10건↑ 성적이 나빠지면 자동 되돌림
    "live_quality": True,          # 🎯 실전 돈은 '강한 신호'에만: 단타 중 프리장 또는 이미 +20%↑ 오른 종목 · 정규장은 거래량 5배↑ · 공시가 확인되면 이유 있는 급등만
    "live_quality_change": 20, "live_quality_ratio": 5,
    "swing_live": False,           # 📉 스윙은 지금 성적이 나빠서(평균 -2.1%) 가상으로만 — 스윙 기록이 좋아지면 켜기
    "live_needs_proof": True,      # 🛡️ 실전 돈은 '검증된 조건'에만: 비슷한 지난 거래 N건↑ · 기대 수익 플러스일 때만 실전, 아니면 가상 장부로 (탐색 매수는 전부 가상)
    "live_proof_n": 10,
    "time_stop_min": 15,           # ⏱️ 시간 손절: 단타를 산 지 이 분 동안 최고 수익이 +아래 % 도 못 가면 바로 정리 (0 = 안 씀)
    "time_stop_up_pct": 0.5,
    "stop_slip_pct": 2,            # 🔻 손절·시간 손절 매도는 현재가보다 이 % 낮게 지정가 → 바로 체결 (손절이 -3% 대신 -4.7% 로 밀리던 것 방지)
    "stop_resell_sec": 30,         #    매도 주문이 이 초 동안 안 팔리면 취소하고 다시 냄
    "surge_min_change_regular": 12,   # 📈 정규장 급등주는 상승률 이 % 이상만 (프리장은 급등 최소 상승률 그대로)
    "surge_buy_cutoff_min": 180,   # 🕒 정규장 시작 이 분 뒤부터는 급등주 새로 안 삼 (0 = 안 씀)
    "open_weak_skip": True,        # 🌅 정규장 초반 + 지수 하락이면 급등주 안 삼
    "open_weak_min": 30,           #    초반 기준(분) (0 = 안 씀)
    "open_weak_idx_pct": 0.5,      #    지수 오늘 -이 % 이하일 때
    "news_on": True,               # 📰 사기 전에 공시 확인 (국장 DART · 미장 SEC) — 악재(증자·CB·감자·상장폐지 등)면 안 삼
    "news_unknown_ratio": 1.0,     #    공시가 하나도 없는 '이유 모르는 급등'이면 금액 × 이 값 (1 = 안 줄임 · 그림자 매매로 0.5·0.25 비교 후 자동 교체)
    "focus_max_pct": 50,           # 🎯 확신할 때만 몰아넣기: 조건 맞으면 한 종목에 예산의 최대 이 %까지 (0 = 안 씀, 늘 똑같이 나눔)
    "focus_min_samples": 20,       #    조건 ① 비슷한 지난 거래가 이 건수 이상
    "focus_min_ev_pct": 2,         #    조건 ② 기대 수익(수수료 뺀 값)이 +이 % 이상
    "ai_chart_check": False,       # 사기 직전 AI 가 1분봉 차트 그림을 보고 찬성/반대 (10~20초 늦어짐, 기본 꺼짐)
    "paper_cash_krw": 1000000,
    "usd_krw": 1400,
    "sim_slip_pct": 0.1,           # 가상 체결 미끄러짐(%) — 가상 매매·그림자·백테스트가 실전보다 좋게 나오지 않게
    "fee_us_pct": 0.1,             # 미장 수수료(%) 사고팔 때 각각
    "fee_kr_pct": 0.015,           # 국장 수수료(%) 사고팔 때 각각
    "tax_kr_pct": 0.18,            # 국장 증권거래세(%) 팔 때만 (세법 바뀌면 고치기)               # 환율 (토스에서 자동 갱신, 실패하면 이 값)
}
NUMS = {"shadow_count": (2, 1000), "promote_min_trades": (3, 200), "promote_margin_pct": (0, 50), "promote_window_days": (1, 90), "ai_max_per_hour": (0, 600),
        "ai_hold_check_min": (1, 120), "stop_min": (0.5, 20), "stop_max": (1, 30), "limit_min_mult": (0.1, 1), "limit_max_mult": (1, 20), "loss_pct_min": (0.5, 20),
        "loss_pct_max": (1, 50), "mode_down_days": (1, 20), "mode_down_month_pct": (1, 50), "mode_up_trades": (3, 100),
        "loss_streak_pause": (1, 20), "pause_min": (5, 600), "monthly_target_pct": (1, 100), "pick_count": (1, 100), "max_total_krw": (10000, 1e10), "max_order_krw": (0, 1e10), "max_position_krw": (0, 1e10),
        "max_orders_per_day": (1, 100), "max_daily_loss_pct": (0.5, 50), "max_daily_loss_krw": (1000, 1e10),
        "stop_loss_pct": (0.5, 50), "take_profit_pct": (0.5, 500), "trail_pct": (0.5, 50), "trail_wide_after_pct": (1, 200), "trail_wide_pct": (0.5, 50), "surge_min_change_pct": (1, 100),
        "surge_max_change_pct": (2, 500), "surge_volume_ratio": (1, 50), "pre_volume_ratio": (0.01, 5), "min_trading_value_krw": (0, 1e13), "us_min_price": (0.1, 100),
        "limit_slip_pct": (0, 3), "min_size_ratio": (0.05, 1), "min_win_prob": (0, 90), "partial_tp_pct": (0, 100), "partial_tp_ratio": (0.1, 0.9), "exit_check_sec": (5, 300), "rsi_max_buy": (30, 90), "swing_stop_pct": (1, 30), "swing_trail_pct": (1, 50), "swing_trail_vol_mult": (0.2, 2), "swing_max_days": (1, 120), "interval_min": (1, 120), "paper_cash_krw": (10000, 1e10), "usd_krw": (500, 3000), "fee_us_pct": (0, 2), "fee_kr_pct": (0, 2), "tax_kr_pct": (0, 2), "sim_slip_pct": (0, 2), "focus_max_pct": (0, 100), "focus_min_samples": (5, 500), "focus_min_ev_pct": (0, 20), "news_unknown_ratio": (0.1, 1), "live_proof_n": (3, 200), "live_quality_change": (0, 100), "live_quality_ratio": (0, 100), "time_stop_min": (0, 240), "time_stop_up_pct": (0, 10), "stop_slip_pct": (0.3, 5),
        "stop_resell_sec": (10, 300), "surge_min_change_regular": (0, 100), "surge_buy_cutoff_min": (0, 390), "open_weak_min": (0, 120), "open_weak_idx_pct": (0, 10),"arena_size": (2, 12), "arena_ai_per_day": (0, 5000), "arena_min_trades": (5, 200), "arena_replay_per_day": (0, 500), "arena_live_n": (0, 5), "arena_live_pct": (5, 100), "arena_ctrl_margin": (0, 5), "arena_half_life_days": (3, 90), "arena_voice_every": (2, 100), "arena_voice_per_day": (0, 50),"regime_half_pct": (0.3, 10), "regime_stop_pct": (0.5, 20), "max_per_sector": (0, 100), "lane_share_surge": (0, 100), "lane_share_min": (0, 50), "lane_share_max": (50, 100), "lane_live_trades": (5, 200), "lane_live_days": (3, 60), "no_rebuy_swing_days": (0, 30), "explore_per_day": (0, 300), "ai_tp_guard_peak": (1, 50), "ai_tp_guard_floor": (0, 10), "ai_tp_min_trades": (3, 200), "ai_tp_margin_pct": (0, 20),
        "chase_near_high_pct": (0.1, 10), "chase_min_pullback_pct": (0, 5), "chase_run5_max_pct": (0.5, 20),
        "rebound_pullback_pct": (0.3, 10), "rebound_pullback_max_pct": (2, 30), "rebound_up_bars": (1, 3)}
CHOICES = {"lane_swing_mode": ("live", "paper", "off"), "lane_surge_mode": ("live", "paper", "off"), "ai_take_profit": ("off", "test", "on"), "mode": ("off", "paper", "live"), "strategy": ("auto", "surge", "trend"), "budget_mode": ("all_cash", "fixed")}
_lock = threading.RLock()
_thread: threading.Thread | None = None
_restart_fn: list = [None]   # web.py 가 넣어 줌 (자동 개선 되돌린 뒤 다시 켜기)
_stop_req = threading.Event()   # 자동 개선 후 다시 켜기 전에: 지금 바퀴만 끝내고 멈춤
_idle = threading.Event()


def pause_for_restart(timeout: float = 90) -> bool:
    """자동매매가 주문 도중에 끊기지 않게, 지금 돌고 있는 바퀴가 끝날 때까지 기다림"""
    _stop_req.set()
    if _thread is None or not _thread.is_alive():
        return True
    return _idle.wait(timeout)


HANDOFF = COMPANY_DIR / "_cache" / "restart_handoff.json"   # 다시 켜기 전에 저장한 상태 요약 (새 프로세스가 읽음)


def wait_idle_for_restart(log=print) -> None:
    """주문·체결 처리 중엔 절대 안 끊음: 바퀴가 끝날 때까지 시간 제한 없이 기다림 (5분마다 안내)"""
    waited = 0
    while not pause_for_restart(60):
        waited += 1
        if waited % 5 == 0:
            log(f"🔄 자동매매 바퀴가 아직 안 끝나서 다시 켜기를 기다리는 중 ({waited}분)")


def resume_after_pause(log=print) -> None:
    """다시 켜기를 못 하게 됐을 때: 멈춘 자동매매 루프를 다시 돌림"""
    HANDOFF.unlink(missing_ok=True)   # 안 껐으니 '이어받음' 기록은 필요 없음
    _stop_req.clear()
    _idle.clear()
    start(log)


def save_for_restart() -> dict:
    """바퀴가 끝난 뒤: 보유·장부(시장별 기록)·학습·그림자 기록을 디스크에 저장하고 요약을 남김"""
    info = {"t": datetime.now().isoformat(timespec="seconds"), "markets": {}}
    with _lock:
        for m in ("US", "KR"):
            if not _state_path(m).exists():
                continue
            with in_market(m):
                s = state()
                _save(s)
                info["markets"][m] = {"bot_pos": len(s.get("bot_pos") or {}),
                                      "paper_pos": len((s.get("paper") or {}).get("positions") or {}),
                                      "orders": len(s.get("orders") or [])}
    with tl._lock:
        tl._save(tl._load())
    try:
        shadow.flush()
    except Exception as e:
        info["shadow_err"] = f"{type(e).__name__}: {e}"
    info["unsaved"] = store.flush()
    try:
        store.write_text(HANDOFF, json.dumps(info, ensure_ascii=False))
    except OSError:
        pass
    return info


def _resume_note() -> None:
    """다시 켜진 뒤: 저장해 둔 상태를 이어받았다고 기록 (상태 자체는 파일에서 그대로 읽힘)"""
    try:
        info = json.loads(HANDOFF.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    HANDOFF.unlink(missing_ok=True)
    parts = [f"{'미장' if m == 'US' else '국장'} 보유 {v['bot_pos']}·가상 {v['paper_pos']}" for m, v in info.get("markets", {}).items()]
    msg = f"♻️ 다시 켜짐 — {info.get('t', '')} 에 저장한 상태 이어받음 ({' · '.join(parts) or '기록 없음'})"
    if info.get("unsaved"):
        msg += f" ⚠️ 못 쓴 파일: {', '.join(info['unsaved'])}"
    _log_fn(msg)
    for m in info.get("markets", {}):
        with in_market(m):
            try:
                event("재시작", msg)
            except Exception:
                pass


_log_fn = print
_busy_m = {"US": threading.Event(), "KR": threading.Event()}   # 시장별 종목 준비/고르기 백그라운드 작업 중
_ctx = threading.local()        # 지금 처리 중인 시장 (스레드마다)


def _raw_cfg() -> dict:
    return dict(store.load(CONFIG))   # 메모리 보관 (파일이 바뀌면 다시 읽음)


def markets(raw: dict | None = None) -> list[str]:
    raw = _raw_cfg() if raw is None else raw
    if int(raw.get("v", 1)) < 4:     # 예전 설정(시장 하나) → 이번 버전부터 국장·미장 둘 다
        return ["US", "KR"]
    ms = [m for m in (raw.get("markets") or DEFAULTS["markets"]) if m in ("US", "KR")]
    return list(dict.fromkeys(ms)) or ["US"]


def cur_market() -> str:
    m = getattr(_ctx, "m", None)
    return m if m in ("US", "KR") else markets()[0]


@contextlib.contextmanager
def in_market(m: str | None):
    """이 안에서는 config()["market"]·state() 가 그 시장 기준"""
    prev = getattr(_ctx, "m", None)
    _ctx.m = m if m in ("US", "KR") else prev
    try:
        yield
    finally:
        _ctx.m = prev


def _busy() -> threading.Event:
    return _busy_m[cur_market()]


# ── 설정·기록 ──
def config() -> dict:
    c = _raw_cfg()
    out = {**DEFAULTS, **{k: v for k, v in c.items() if k in DEFAULTS}}
    out["markets"] = markets(c)
    out["market"] = cur_market() if cur_market() in out["markets"] else out["markets"][0]
    if int(c.get("v", 1)) < 2:   # 예전 기본값(2분)을 그대로 쓰던 경우 → 새 기본값 1분
        if c.get("interval_min") == 2:
            out["interval_min"] = 1
    if int(c.get("v", 1)) < 3:   # 나눠 팔기 예전 기본값(+6%) → 새 기본값(+4%)
        if c.get("partial_tp_pct") in (6, 6.0):
            out["partial_tp_pct"] = 4.0
    if int(c.get("v", 1)) < 4:   # 이번 버전: 수익률 기준 자동 전략 · 국장+미장
        out["strategy"] = "auto"
    if int(c.get("v", 1)) < 5:   # 손절 확인 10초 → 5초 (손절이 늦게 체결되던 문제)
        if c.get("exit_check_sec") in (10, 10.0, None):
            out["exit_check_sec"] = 5
    if int(c.get("v", 1)) < 6:   # 🏟️ AI 리그 참가자 5 → 10
        if c.get("arena_size") in (5, None):
            out["arena_size"] = 10
    if int(c.get("v", 1)) < 7:   # 🏟️ 가상 표본 제한 풀기 (AI 리그 판단 횟수 무제한)
        if c.get("arena_ai_per_day") in (40, 60, None):
            out["arena_ai_per_day"] = 0
    if int(c.get("v", 1)) < 8:   # 🧪 그림자 조합 60 → 30 (비교 대상이 많으면 우연히 좋아 보이는 조합이 섞여서)
        if c.get("shadow_count") in (60, None):
            out["shadow_count"] = 30
    out["v"] = 8
    return out


def note_user_mode(mode: str) -> None:
    """사장님이 직접 모드를 바꾼 기록 (실전을 직접 켠 적이 있어야 자동 복귀 허용, 직접 바꾸면 자동 전환 표시 해제)"""
    with _lock:
        s = state()
        if mode == "live":
            s["user_live_ok"] = True
        s["auto_paper"] = False
        _save(s)


def save_config(new: dict, _by_auto: bool = False, _revert: bool = False) -> dict:
    c, errors = config(), []
    if _by_auto and not _revert:   # 🧮 자동 규칙 변경은 하루 1번 (안전·배분은 예외)
        ok_g, why_g = extras.rule_gate([k for k in new if c.get(k) != new[k]])
        if not ok_g:
            return {"ok": False, "message": why_g}
    before_ = dict(c)
    if "mode" in new and not _by_auto and new.get("mode") in CHOICES["mode"]:
        note_user_mode(new["mode"])
    for k, opts in CHOICES.items():
        if k in new:
            if new[k] not in opts:
                errors.append(f"{k} 는 {'/'.join(opts)} 중 하나")
            elif k == "mode" and new[k] == "live" and not toss.enabled():
                errors.append("실전은 토스 키(🧰 도구)를 넣은 뒤에 켤 수 있어요")
            else:
                c[k] = new[k]
    if "markets" in new:
        ms = new["markets"] if isinstance(new["markets"], list) else str(new["markets"]).replace(",", " ").split()
        ms = [m for m in dict.fromkeys(str(x).upper() for x in ms) if m in ("US", "KR")]
        if not ms:
            errors.append("시장을 하나 이상 골라 주세요")
        else:
            c["markets"] = ms
    if "exclude" in new:
        raw = new["exclude"] if isinstance(new["exclude"], list) else str(new["exclude"]).replace(",", " ").split()
        ex = [toss.symbol(s) for s in raw if str(s).strip()]
        if any(not re.fullmatch(r"\d{6}|[A-Z][A-Z.\-]{0,9}", x) for x in ex):
            errors.append("제외 종목 코드가 이상해요")
        else:
            c["exclude"] = list(dict.fromkeys(ex))[:100]
    if "watchlist" in new:
        raw = new["watchlist"] if isinstance(new["watchlist"], list) else str(new["watchlist"]).replace(",", " ").split()
        wl = [toss.symbol(s) for s in raw if str(s).strip()]
        if any(not re.fullmatch(r"\d{6}|[A-Z][A-Z.\-]{0,9}", x) for x in wl):
            errors.append("관심 종목 코드가 이상해요 (예: NVDA, 005930)")
        else:
            c["watchlist"] = list(dict.fromkeys(wl))[:30]
    if "symbols" in new:
        raw = new["symbols"] if isinstance(new["symbols"], list) else str(new["symbols"]).replace(",", " ").split()
        syms = [toss.symbol(s) for s in raw if s.strip()]
        bad = [s for s in syms if not re.fullmatch(r"\d{6}|[A-Z][A-Z.\-]{0,9}", s)]
        if bad:
            errors.append("종목 코드가 이상해요: " + ", ".join(bad))
        else:
            c["symbols"] = list(dict.fromkeys(syms))[:20]
    for k, (lo, hi) in NUMS.items():
        if k in new and new[k] not in (None, ""):
            try:
                v = float(new[k])
                if not lo <= v <= hi:
                    raise ValueError
                c[k] = int(v) if v.is_integer() and isinstance(DEFAULTS[k], int) else v
            except (TypeError, ValueError):
                errors.append(f"{k} 는 {lo:g}~{hi:g}")
    for k in [k for k, v in DEFAULTS.items() if isinstance(v, bool)]:   # 켜기/끄기 설정은 전부 (새로 추가한 것도 빠짐없이)
        if k in new:
            v = new[k]
            c[k] = v.strip().lower() in ("1", "true", "on", "yes", "켜기", "예") if isinstance(v, str) else bool(v)
    if "backup_dir" in new:   # 글자 설정
        c["backup_dir"] = str(new["backup_dir"] or "").strip()[:300]
    if errors:
        return {"ok": False, "message": " · ".join(errors)}
    c.pop("market", None)
    store.save(CONFIG, c)
    if _by_auto:
        try:
            extras.rule_record(before_, c, revert=_revert)
        except Exception:
            pass
    event("설정", f"모드 {c['mode']} · {'+'.join({'US': '미장', 'KR': '국장'}[m] for m in c['markets'])} · {STRAT_NAME[c['strategy']]} · "
                f"{'현금 전부' if c['budget_mode'] == 'all_cash' else '한도 ' + format(c['max_total_krw'], ',') + '원'}"
                + ("" if c["auto_pick"] else f" · 종목 {', '.join(c['symbols']) or '없음'}"))
    return {"ok": True, "config": c}


STRAT_NAME = {"auto": "수익률 기준(단타+스윙)", "surge": "급등주", "trend": "추세"}


def _state_path(m: str | None = None):
    """시장마다 기록 파일 따로 (예전 하나짜리 파일은 미장 것으로 옮김)"""
    m = m or cur_market()
    path = STATE.with_name(f"autotrade_state_{m}.json")
    if m == "US" and not path.exists() and STATE.exists():
        try:
            STATE.rename(path)
        except OSError:
            pass
    return path


def _fresh_day(s: dict, market: str) -> dict:
    day = picker.session_date(market)
    if s.get("day") != day:  # 장(현지 날짜)이 바뀌면 하루 카운트·정지·손익 초기화 (스윙 종목의 최고가는 유지)
        keep = {k: v for k, v in (s.get("highs") or {}).items() if (s.get("style") or {}).get(k) == "swing"}
        s.update(day=day, orders_today=0, halted_today="", realized_today=0.0, highs=keep, day_budget=0.0, explore_today=0,
                 realized_lane={}, realized_book={}, halted_lane={})
        nr = s.get("no_rebuy")
        s["no_rebuy"] = {k: v for k, v in (nr if isinstance(nr, dict) else {}).items() if str(v or "") >= day}   # 기한 지난 재진입 금지는 풀기
    return s


def state() -> dict:
    with _lock:
        s = store.load(_state_path())   # 메모리 보관 · 깨지면 백업에서 복구
        for k, v in (("events", []), ("orders", []), ("bot_pos", {}), ("picks", {}), ("swing", {}), ("style", {}), ("buy_day", {}), ("highs", {}), ("realized_today", 0.0), ("month", {}), ("partial", {}), ("trail_mode", {}), ("hold_log", {}), ("no_rebuy", {})):
            s.setdefault(k, v)
        if "paper" not in s:
            s["paper"] = {"cash": config()["paper_cash_krw"], "positions": {}, "realized": 0}
        for k, v in list(s["bot_pos"].items()):  # 예전 형식(수량만) 호환
            if not isinstance(v, dict):
                s["bot_pos"][k] = {"qty": float(v), "avg": 0.0}
        return _fresh_day(s, cur_market())


def _save(s: dict) -> None:
    s["events"] = s["events"][-400:]
    s["orders"] = s["orders"][-400:]
    store.save(_state_path(), s)   # 임시 파일에 쓰고 바꿔치기 + 백업


def event(kind: str, text: str, **extra) -> None:
    with _lock:
        s = state()
        s["events"].append({"t": datetime.now(KST).strftime("%m-%d %H:%M:%S"), "kind": kind, "text": text, **extra})
        _save(s)
    flag = '🇺🇸' if cur_market() == 'US' else '🇰🇷'
    _log_fn(f"🤖 {flag} [{kind}] {text}")
    if kind in NOTIFY_KINDS and not (kind == "자동 조정" and not text.startswith(("오늘 자동으로", "모드"))) \
            and not (kind == "실데이터" and text.startswith("점검 통과")):
        from . import notify
        notify.send(f"{flag} [{kind}] {text}")


NOTIFY_KINDS = {"판단 점검", "AI 리그", "노트 정리","교차 복기", "익절 비교", "공시 악재", "실전 주문", "체결", "주문 거절", "정지", "긴급 정지", "정지 해제", "규칙 교체", "자동 조정", "복기", "실데이터",
                "휴장", "오류", "복구", "AI 판단", "백테스트", "확대 조건", "반등 비교", "검증 요약", "월 수익"}   # 🔔 텔레그램으로 보낼 기록 종류

MONTH_ALERT_PCT = 20.0   # 🎉 이번 달 실현 수익률이 이걸 처음 넘으면 알림 (시장별 월 1회, 알림만)


def _month_alert() -> None:
    """이번 달 실현 수익률(시작 예산 대비) +20% 첫 돌파 알림 — 모드·설정은 안 바꿈"""
    try:
        m = cur_market(); key = datetime.now(KST).strftime("%Y-%m")
        with _lock:
            s = state(); mo = s.get("month") or {}
            start = float(mo.get("start") or 0)
            if mo.get("key") != key or start <= 0 or mo.get("alert20"):
                return
            realized = float(mo.get("realized", 0))
            pct = realized / start * 100
            if pct <= MONTH_ALERT_PCT:
                return
            mo["alert20"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M"); _save(s)   # 이번 달 한 번만
        st_ = tl.month_closed_stats(m, key)
        event("월 수익", f"이번 달 실현 수익률 {pct:+.1f}% (시작 예산 {start:,.0f}원 대비 {realized:+,.0f}원) — +{MONTH_ALERT_PCT:g}% 첫 돌파 · "
              f"끝난 거래 {st_['n']}건 · 승률 {st_['win_rate']}% ({st_['wins']}승) · 알림만, 모드는 그대로예요")
    except Exception as e:
        _log_fn(f"🤖 [오류] 월 수익 알림: {type(e).__name__}: {e}")


def _mkt(sym: str) -> str:
    return "KR" if re.fullmatch(r"\d{6}", sym) else "US"


def _ext(market: str, c: dict | None = None) -> bool:
    return market == "US" and bool((c or config()).get("us_premarket"))


def minutes_now(market: str, c: dict | None = None) -> int | None:
    return picker.session_minutes(market, extended=_ext(market, c))


def market_open(sym: str, side: str = "BUY", now: datetime | None = None) -> bool:
    """매수: (프리장 켜면 뉴욕 04:10~ / 아니면 정규장 시작 10분 뒤~) ~ 마감 30분 전
    매도: 장(프리장 포함) 시작 1분 뒤 ~ 마감 2분 전 (휴장일은 토스가 거절 → 건너뜀)"""
    mk = _mkt(sym)
    m = picker.session_minutes(mk, now, extended=_ext(mk))
    if m is None:
        return False
    first = -330 if _ext(mk) else 0
    return (first + 10 <= m <= 360) if side == "BUY" else (first + 1 <= m <= 388)


# ── 환율 ──
_fx_cache = [0.0, 0.0]


def usd_krw(c: dict) -> float:
    if time.time() - _fx_cache[0] < 600 and _fx_cache[1]:
        return _fx_cache[1]
    rate = float(c["usd_krw"])
    try:
        r = toss._get("/api/v1/exchange-rate", baseCurrency="USD", quoteCurrency="KRW") or {}
        for k in ("rate", "exchangeRate", "basePrice", "price", "value"):
            if r.get(k):
                rate = float(r[k]); break
    except (toss.TossError, ValueError, TypeError):
        pass
    if 500 <= rate <= 3000:
        _fx_cache[:] = [time.time(), rate]
    return rate


# ── 규칙 ──
def _ma(xs: list[float], n: int) -> float | None:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def _rsi(xs: list[float], n: int = 14) -> float | None:
    if len(xs) <= n:
        return None
    g = l = 0.0
    for a, b in zip(xs[-n - 1:-1], xs[-n:]):
        g += max(b - a, 0); l += max(a - b, 0)
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def swing_feats(sym: str, price: float, fx: float, day: str | None = None) -> dict:
    """스윙 진입 조건 (일지·학습용, 매수 판단엔 안 씀): 20일·60일 등락률, RSI, 20일 평균 거래대금(원), 변동성(연율 %),
    당일 등락률, 거래량 배수(당일 ÷ 20일 평균). day(장 날짜)를 주면 그날 기준으로 계산 (예전 기록 보충용)"""
    closes, vols, dates = toss.daily(sym, 100)
    d0 = day or picker.session_date(_mkt(sym))
    prev = [i for i, d in enumerate(dates) if str(d)[:10] < d0]   # 그날 전까지의 일봉
    if len(prev) < 61 or not price:
        return {}
    pc, pv = [closes[i] for i in prev], [vols[i] for i in prev]
    today_vol = next((vols[i] for i, d in enumerate(dates) if str(d)[:10] == d0), 0.0)
    series = pc + [price]
    rets = [math.log(b / a) for a, b in zip(series[-21:-1], series[-20:]) if a > 0 and b > 0]
    rsi = _rsi(series)
    v20 = sum(pv[-20:]) / 20
    return {"r20": round((price / series[-21] - 1) * 100, 1), "r60": round((price / series[-61] - 1) * 100, 1),
            "rsi": round(rsi) if rsi is not None else None,
            "value20": round(sum(c_ * v for c_, v in zip(pc[-20:], pv[-20:])) / 20 * (1 if _mkt(sym) == "KR" else fx)),
            "volat": round((sum(x * x for x in rets) / max(len(rets), 1)) ** 0.5 * math.sqrt(252) * 100, 1),
            "chg1d": round((price / pc[-1] - 1) * 100, 1) if pc[-1] else None,
            "vratio": round(today_vol / v20, 2) if today_vol and v20 else None}


def vol_trend(sym: str) -> dict:
    """진입 시점 거래량 추세 (기록용): 1분봉 최근 5분 평균 ÷ 그 전 10분 평균 → 증가/감소/보합"""
    from . import chart
    cs = chart.candles(sym, "1m", 16)[:-1]   # 마지막(진행 중) 봉은 뺌
    if len(cs) < 15:
        return {}
    a, b = sum(x["v"] for x in cs[-5:]) / 5, sum(x["v"] for x in cs[-15:-5]) / 10
    if not b:
        return {}
    r = a / b
    return {"vtrend": "증가" if r >= 1.2 else "감소" if r <= 0.8 else "보합", "vtrend_r": round(r, 2)}


def explore_value_ok(sym: str, price: float, fx: float, c: dict) -> tuple[bool, str]:
    """🔬 탐색 매수용 최소 거래대금 확인: 20일 평균·오늘 거래대금 중 큰 값이 기준 이상이어야 삼 (확인 못 하면 안 삼)"""
    need = float(c["min_trading_value_krw"])
    if need <= 0:
        return True, ""
    try:
        closes, vols, dates = toss.daily(sym, 25)
    except Exception as e:
        return False, f"거래대금 확인 오류({type(e).__name__})"
    f = 1 if _mkt(sym) == "KR" else fx
    d0 = picker.session_date(_mkt(sym))
    past = [(cl, v) for cl, v, d in zip(closes, vols, dates) if str(d)[:10] < d0][-20:]
    v20 = sum(cl * v for cl, v in past) / 20 * f if len(past) >= 20 else 0.0
    vt = next((v for v, d in zip(vols, dates) if str(d)[:10] == d0), 0.0) * price * f
    best = max(v20, vt)
    note = f"거래대금 20일 평균 {v20 / 1e8:,.0f}억 · 오늘 {vt / 1e8:,.0f}억 (기준 {need / 1e8:,.0f}억)"
    return best >= need, note


_bf_done: set[str] = set()   # 보충을 시도한 거래 id


def _backfill_swing(mode_key: str, market: str, fx: float, limit: int = 3) -> list[str]:
    """보유 중 스윙 거래의 빈 진입 조건을 산 날 차트로 채움 (한 바퀴에 몇 개씩, 실패하면 다음 바퀴에 다시)"""
    out = []
    for t in [x for x in tl.swing_missing(mode_key, market) if x["id"] not in _bf_done][:limit]:
        try:
            dt = datetime.strptime(t["entry_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
            f = swing_feats(t["sym"], float(t["entry_px"]), fx, picker.session_date(market, dt))
        except (toss.TossError, ValueError, OSError):
            continue   # 조회 실패 → 다음 바퀴에 다시
        _bf_done.add(t["id"])   # 차트를 받았으면 이번 실행에선 다시 안 함 (데이터가 모자라 빈 값이 남아도)
        if f and tl.fill_features(t["id"], f, "산 날 일봉으로 보충 (거래량 배수는 그날 하루 전체 기준)"):
            out.append(f"📚 {t['sym']} 스윙 진입 조건 보충 (20일 {f.get('r20')}% · RSI {f.get('rsi')})")
    return out


def signal_trend(closes: list[float], price: float, qty: float, avg: float, c: dict) -> tuple[str, str]:
    series = closes + [price]
    ma20, ma60, rsi = _ma(series, 20), _ma(series, 60), _rsi(series)
    if qty > 0 and avg > 0:
        pnl = (price / avg - 1) * 100
        if pnl <= -c["stop_loss_pct"]:
            return "SELL", f"손절: 평단 대비 {pnl:.1f}%"
        if pnl >= c["take_profit_pct"]:
            return "SELL", f"익절: 평단 대비 +{pnl:.1f}%"
        if ma20 and price < ma20 * 0.99:
            return "SELL", f"추세 이탈: 20일선({ma20:,.2f}) 아래"
        return "HOLD", f"보유 유지 (평단 대비 {pnl:+.1f}%)"
    if not (ma20 and ma60 and rsi is not None):
        return "HOLD", "차트 데이터 부족"
    if price > ma20 > ma60 and 45 <= rsi <= c["rsi_max_buy"]:
        return "BUY", f"상승 추세: 현재가 > 20일선 > 60일선, RSI {rsi:.0f}"
    return "HOLD", f"조건 안 맞음 (RSI {rsi:.0f})"


VOL_TRAIL_MIN, VOL_TRAIL_MAX = 3.0, 8.0   # 변동성 기준 허용 범위(%) — 밖이면 swing_trail_pct


def swing_trail_for(closes: list[float], price: float, c: dict) -> tuple[float, str]:
    """스윙 고점 대비 기준(%): swing_trail_vol 켜면 최근 20일 하루 변동성 × 배수, 3~8% 밖·데이터 부족이면 swing_trail_pct"""
    base = float(c["swing_trail_pct"])
    if not c.get("swing_trail_vol"):
        return base, ""
    s = [x for x in (list(closes or [])[-20:] + [price]) if x and x > 0]
    rets = [math.log(b / a) for a, b in zip(s[:-1], s[1:])]
    if len(rets) < 10:
        return base, " · 변동성 데이터 부족→기본"
    dv = (sum(x * x for x in rets) / len(rets)) ** 0.5 * 100   # 하루 변동성(%)
    t = round(dv * float(c.get("swing_trail_vol_mult", 0.7)), 1)
    if VOL_TRAIL_MIN <= t <= VOL_TRAIL_MAX:
        return t, f" · 변동성 연 {dv * math.sqrt(252):.0f}%"
    return base, f" · 변동성 기준 {t:g}% 범위 밖→기본"


def signal_swing(closes: list[float], price: float, qty: float, avg: float, high: float, days: int, c: dict) -> tuple[str, str]:
    """스윙(며칠 보유) 매도: 손절 · 고점 대비 하락(최고 수익이 기준 이상 난 뒤) · 20일선 이탈 · 최대 보유 일수. 밤새 들고 감"""
    pnl = (price / avg - 1) * 100
    high = max(high or price, price)
    peak, drop = (high / avg - 1) * 100, (price / high - 1) * 100
    ma20 = _ma(closes + [price], 20)
    trail, tnote = swing_trail_for(closes, price, c)
    if pnl <= -c["swing_stop_pct"]:
        return "SELL", f"[스윙] 손절: 평단 대비 {pnl:.1f}% (기준 -{c['swing_stop_pct']:g}%)"
    if peak >= trail and drop <= -trail:
        return "SELL", f"[스윙] 고점 대비 {drop:.1f}% 하락 (기준 -{trail:g}%{tnote}, 최고 +{peak:.1f}%) → 수익 지키기 (평단 대비 {pnl:+.1f}%)"
    if ma20 and price < ma20 * 0.98:
        return "SELL", f"[스윙] 추세 이탈: 20일선({ma20:,.2f}) 아래 (평단 대비 {pnl:+.1f}%)"
    if days >= c["swing_max_days"]:
        return "SELL", f"[스윙] {days}일 보유 → 정리 (평단 대비 {pnl:+.1f}%)"
    return "HOLD", f"[스윙] 보유 유지 {days}일째 (평단 대비 {pnl:+.1f}% · 최고 +{peak:.1f}%)"


_news_seen: dict[str, float] = {}


def _seen_news_block(sym: str) -> bool:
    """같은 종목 악재 알림은 1시간에 한 번만"""
    if time.time() - _news_seen.get(sym, 0) < 3600:
        return True
    _news_seen[sym] = time.time()
    return False


# ── 🤝 두 AI (1호 일반=스윙 · 2호 급등주=단타) ──
LANES = {"swing": "1호 일반(스윙)", "surge": "2호 급등주(단타)"}
PERSONA = {"swing": "너는 1호 '일반 매매' AI 다: 며칠 보유하는 추세 스윙. 추세·거래대금·실적/공시 흐름을 중시하고, 급등 추격은 하지 않는다.",
           "surge": "너는 2호 '급등주' AI 다: 당일 정리 단타. 거래량 폭증·상승 초입·가격 탄력을 중시하고, 꼭대기 추격·거래량 감소 때는 사지 않는다."}


def lane_mode(c: dict, style: str) -> str:
    return str(c.get(f"lane_{style}_mode", "live"))


def lane_live(c: dict, style: str) -> bool:
    """이 AI 의 새 매수가 실전인지 — 전체 모드가 실전이고 이 AI 도 live 일 때만 (가상 AI 는 절대 실전 주문 못 냄)"""
    return c["mode"] == "live" and lane_mode(c, style) == "live"


def lane_book(c: dict, style: str) -> str:
    return "live" if lane_live(c, style) else "paper"


def lane_share(c: dict, style: str) -> float:
    """같은 장부를 쓰는 AI 들 사이의 예산 몫 (한 AI 만 그 장부를 쓰면 1.0)"""
    on = [st for st in LANES if lane_book(c, st) == lane_book(c, style) and lane_mode(c, st) != "off"]
    if len(on) < 2:
        return 1.0
    sh = min(1.0, max(0.0, float(c.get("lane_share_surge", 50)) / 100))
    return sh if style == "surge" else 1.0 - sh


def lane_persona(style: str, market: str) -> str:
    """AI 판단 프롬프트 앞에 붙일 성향 + 공유 노트(두 AI 가 같이 쓰는 교훈)"""
    notes = tl.lessons_text(market, 6)
    champ = ""
    if style == "surge" and config().get("arena_promote"):
        try:
            champ = arena.champion_text(market)
        except Exception:
            champ = ""
    return PERSONA.get(style, "") + (f"\n공유 노트(두 AI 가 같이 배운 것): {notes}" if notes else "") + (f"\n{champ}" if champ else "")


def _add_realized(s: dict, sym: str, gain: float, book: str) -> None:
    """실현 손익을 전체·AI별·장부별로 같이 쌓기"""
    s["realized_today"] = float(s.get("realized_today", 0)) + gain
    st = (s.get("style") or {}).get(sym) or "surge"
    rl = s.setdefault("realized_lane", {}); rl[st] = float(rl.get(st, 0)) + gain
    rb = s.setdefault("realized_book", {}); rb[book] = float(rb.get(book, 0)) + gain


def lane_stats(market: str, style: str, days: int = 14) -> dict:
    """이 AI 의 최근 성적 (실전+가상 합산, 수수료 뺀 값) + 장부별 건수"""
    since = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    with tl._lock:
        ts = [t for t in tl._load()["trades"] if t["closed"] and tl._tm(t) == market and tl._ts(t) == style
              and str(t.get("exits", [{}])[-1].get("t", t.get("entry_time", ""))) >= since]
    st = tl._stats(ts)
    st.update(n_live=sum(1 for t in ts if t["mode"] == "live"), n_paper=sum(1 for t in ts if t["mode"] == "paper"), days=days)
    return st


def _lane_day_sums(market: str, style: str, n: int) -> list[float]:
    """이 AI 의 최근 n일 하루 손익 합(%) — 오래된 것 → 최근"""
    with tl._lock:
        ts = [t for t in tl._load()["trades"] if t["closed"] and tl._tm(t) == market and tl._ts(t) == style]
    by: dict[str, float] = {}
    for t in ts:
        by[t["day"]] = by.get(t["day"], 0.0) + float(t["pnl_pct"])
    return [by[d] for d in sorted(by)[-n:]]


def lanes_overview(market: str) -> dict:
    c = config()
    out = {}
    from . import statwin as sw   # 📏 화면용: 기준 이후 성적 (자동 조정 판단은 lane_stats 그대로)
    with tl._lock:
        ts_ = [t for t in tl._load()["trades"] if t["closed"] and tl._tm(t) == market]
    for st, name in LANES.items():
        out[st] = {"name": name, "mode": lane_mode(c, st), "live": lane_live(c, st), "book": lane_book(c, st),
                   "share": round(lane_share(c, st) * 100), **lane_stats(market, st, int(c.get("lane_live_days", 14))),
                   "ss": sw.view([t for t in ts_ if tl._ts(t) == st])}
    return out


def _no_tp(c: dict) -> dict:
    """익절 규칙(나눠 팔기·전부 익절·고점 대비 하락)만 끈 설정 — 손절·마감 정리·본전 정리·20일선·최대 일수는 그대로"""
    return {**c, "partial_tp_pct": 0, "take_profit_pct": 0, "trail_pct": 1e9, "trail_wide_pct": 1e9, "trail_wide_after_pct": 0,
            "swing_trail_pct": 1e9, "swing_trail_vol": False}


def tp_guard(avg: float, high: float, price: float, c: dict) -> str:
    """AI 익절 안전장치: 최고 +3% 이상 → +0.3% 까지 밀리면 무조건 팔기"""
    if not avg:
        return ""
    peak, pnl = (high / avg - 1) * 100, (price / avg - 1) * 100
    if peak >= c["ai_tp_guard_peak"] and pnl <= c["ai_tp_guard_floor"]:
        return f"🛡️ AI 익절 안전장치: 최고 +{peak:.1f}% → 지금 {pnl:+.1f}% (기준 +{c['ai_tp_guard_floor']:g}%) → 팔기"
    return ""


def _ai_room(c: dict) -> bool:
    """이번 시간 AI 판단 여유가 있는지 (횟수는 안 씀)"""
    with _lock:
        n = len([t for t in state().get("ai_calls", []) if time.time() - t < 3600])
    return n < c["ai_max_per_hour"]


def _ghost_open(sym: str, held: dict, high: float, price: float, style: str, bought: str | None) -> None:
    """test 모드: 규칙이 익절했을 때 'AI 였으면?'을 이어서 따라가는 유령 보유 (돈 안 씀)"""
    with _lock:
        s = state()
        g = s.setdefault("tp_ghost", {})
        if sym not in g:
            g[sym] = {"avg": held["avg"], "high": max(high, price), "rule_px": price, "style": style, "buy_day": bought,
                      "t": datetime.now(KST).strftime("%m-%d %H:%M"), "ai_at": 0}
            _save(s)


def _ghost_step(c: dict, market: str, minutes) -> list[str]:
    """유령 보유 따라가기: AI 익절 모드였으면 언제 팔았을지 → 규칙 익절과 차이 기록 → 충분히 나으면 자동으로 on"""
    out = []
    with _lock:
        ghosts = dict(state().get("tp_ghost", {}))
    if not ghosts:
        return out
    try:
        qs = toss.prices(list(ghosts))
    except toss.TossError:
        return out
    nt = _no_tp(c)
    for sym, g in ghosts.items():
        q = qs.get(sym)
        if not q:
            continue
        price = q["price"]
        g["high"] = max(g["high"], price)
        why = tp_guard(g["avg"], g["high"], price, c)
        if not why:
            if g["style"] == "swing":
                try:
                    closes, _, _ = toss.daily(sym, 60)
                except toss.TossError:
                    continue
                days = (datetime.now(KST).date() - datetime.strptime(g["buy_day"], "%Y-%m-%d").date()).days if g.get("buy_day") else 0
                side, r = signal_swing(closes, price, 1, g["avg"], g["high"], days, nt)
            elif minutes is None:
                side, r = "SELL", "장 끝 (단타)"
            else:
                side, r = signal_surge(price, 1, g["avg"], g["high"], minutes, False, nt)
            if side == "SELL":
                why = "규칙(손절·마감 등): " + r
        if not why:
            dec = _ai_peek(("ghost", market, sym))
            if dec and dec != "PENDING" and dec[0]:
                why = "🤖 AI: " + dec[1]
                _ai_jobs.pop(("ghost", market, sym), None)
            elif dec is None and time.time() - g.get("ai_at", 0) >= c["ai_hold_check_min"] * 60 and _ai_room(c):
                g["ai_at"] = time.time()
                _ai_async(("ghost", market, sym), _ai_decide_hold, sym, {"avg": g["avg"], "qty": 1}, price, g["high"], g["style"],
                          "익절 비교용 (규칙은 이미 팔았음)", c)
        with _lock:
            s = state()
            gs = s.setdefault("tp_ghost", {})
            if why:
                gs.pop(sym, None)
                diff = (price - g["rule_px"]) / g["avg"] * 100
                st_ = s.setdefault("ai_tp_stats", {"n": 0, "sum": 0.0, "log": []})
                st_["n"] += 1; st_["sum"] += diff
                st_["log"] = (st_["log"] + [{"t": g["t"], "sym": sym, "diff": round(diff, 2), "why": why[:60]}])[-30:]
                out.append(f"🤖 익절 비교 {sym}: AI 였으면 {diff:+.2f}%p ({why[:50]})")
                event("익절 비교", f"{sym} 규칙 익절 {g['rule_px']:g} vs AI {price:g} → AI {diff:+.2f}%p · 누적 {st_['n']}건 평균 {st_['sum'] / st_['n']:+.2f}%p", symbol=sym)
            elif sym in gs:
                gs[sym] = g
            _save(s)
    # 자동 전환: 두 시장 합쳐서 충분히 쌓이고 AI 가 확실히 나으면 on
    n = tot = 0
    for m in ("US", "KR"):
        with in_market(m):
            st_ = state().get("ai_tp_stats") or {}
        n += st_.get("n", 0); tot += st_.get("sum", 0.0)
    if c["ai_take_profit"] == "test" and n >= c["ai_tp_min_trades"] and tot / n >= c["ai_tp_margin_pct"]:
        if save_config({"ai_take_profit": "on"}, _by_auto=True).get("ok"):
            event("규칙 교체", f"🤖 익절을 AI 판단으로 바꿨어요 — 비교 {n}건 평균 AI 쪽이 {tot / n:+.2f}%p 나음 (안전장치 +{c['ai_tp_guard_peak']:g}%→+{c['ai_tp_guard_floor']:g}%)")
    return out


def time_stop(bought_ts, avg: float, high: float, price: float, c: dict) -> str:
    """⏱️ 시간 손절: 산 지 N분 동안 최고 수익이 +X% 도 못 갔으면 '안 오르는 종목' → 손절선까지 안 기다리고 정리"""
    n = float(c.get("time_stop_min", 0) or 0)
    if n <= 0 or not bought_ts or not avg:
        return ""
    mins = (time.time() - float(bought_ts)) / 60
    peak, pnl = (high / avg - 1) * 100, (price / avg - 1) * 100
    if mins >= n and peak < float(c.get("time_stop_up_pct", 0.5)):
        return f"⏱️ 시간 손절: 산 지 {mins:.0f}분 동안 최고 {peak:+.1f}% (기준 +{c.get('time_stop_up_pct', 0.5):g}%) → 안 오르는 종목이라 정리 (평단 대비 {pnl:+.1f}%)"
    return ""


def focus_ok(est: dict | None, c: dict) -> bool:
    """🎯 확신할 때만 몰아넣기 조건: 비슷한 지난 거래가 충분히 많고(N건) 기대 수익이 높을 때(+X%)만"""
    return bool(est and c.get("focus_max_pct") and est.get("n", 0) >= c["focus_min_samples"]
                and est.get("ev", 0) >= c["focus_min_ev_pct"])


def trail_for(avg: float, high: float, c: dict) -> float:
    """이 종목에 지금 적용할 고점 대비 하락 기준(%) — 최고 수익이 trail_wide_after_pct 이상이면 넓은 기준"""
    peak = (high / avg - 1) * 100 if avg and high else 0
    return c["trail_wide_pct"] if c.get("trail_wide_after_pct") and peak >= c["trail_wide_after_pct"] else c["trail_pct"]


def signal_surge(price: float, qty: float, avg: float, high: float, minutes: int | None, picked: bool, c: dict,
                 partial_done: bool = False) -> tuple[str, str]:
    """('BUY'|'SELL'|'SELL_PART'|'HOLD', 이유)
    매도 순서: 마감 정리 → 손절 → (나눠 판 뒤) 본전 정리 → 나눠 팔기 → (나눠 판 뒤 또는 안 쓸 때) 전부 익절 → 고점 대비 하락"""
    if qty > 0 and avg > 0:
        pnl = (price / avg - 1) * 100
        high = max(high or price, price)
        peak = (high / avg - 1) * 100
        drop = (price / high - 1) * 100
        trail = trail_for(avg, high, c)
        if minutes is not None and minutes >= 375:
            return "SELL", f"장 마감 15분 전 정리 (평단 대비 {pnl:+.1f}%)"
        if pnl <= -c["stop_loss_pct"]:
            return "SELL", f"손절: 평단 대비 {pnl:.1f}% (기준 -{c['stop_loss_pct']}%)"
        if partial_done and pnl <= c["limit_slip_pct"] + 0.2:   # 일부 익절한 뒤엔 남은 물량을 손해 보고 팔지 않게 (파는 지정가 -0.5% 감안해 본전에서 정리)
            return "SELL", f"나눠 팔기 후 본전 도달 → 남은 물량 정리 (평단 대비 {pnl:+.1f}%)"
        if not partial_done and c.get("partial_tp_pct") and pnl >= c["partial_tp_pct"]:
            return "SELL_PART", f"나눠 팔기: 평단 대비 +{pnl:.1f}% → {int(c['partial_tp_ratio'] * 100)}% 먼저 익절"
        # 남은 물량 전부 익절: 나눠 판 뒤에도 작동 (예전엔 나눠 팔기를 쓰면 익절이 영영 안 돼서 고점 대비 하락만 기다리던 버그)
        if c.get("take_profit_pct") and pnl >= c["take_profit_pct"] and (partial_done or not c.get("partial_tp_pct")):
            return "SELL", f"익절: 평단 대비 +{pnl:.1f}% (기준 +{c['take_profit_pct']:g}%{' · 나눠 판 뒤 남은 물량' if partial_done else ''})"
        # 고점 대비 하락: 최고 수익이 기준(trail) 이상 났으면 '무장' → 그 뒤엔 평단 아래로 살짝 내려가도 팔기
        # (예전엔 '지금 수익 중'일 때만 팔아서, +4.4% → 평단 아래로 빠르게 밀리면 손절(-3%)까지 기다리던 문제 수정)
        if peak >= trail and drop <= -trail:
            return "SELL", f"고점 대비 {drop:.1f}% 하락 (기준 -{trail:g}%, 최고 +{peak:.1f}%) → 수익 지키기 (평단 대비 {pnl:+.1f}%)"
        armed = "작동 중" if peak >= trail else f"최고 +{peak:.1f}% < {trail:g}% 라 아직 대기"
        note = f" · 이미 나눠 팔았음 → 남은 물량은 +{c['take_profit_pct']:g}%에서 익절" \
            if partial_done and c.get("partial_tp_pct") and pnl >= c["partial_tp_pct"] else ""
        return "HOLD", f"보유 유지 (평단 대비 {pnl:+.1f}% · 최고 +{peak:.1f}% · 고점 대비 {drop:.1f}% / 기준 -{trail:g}% {armed}{note})"
    if not picked:
        return "HOLD", "선택 종목 아님"
    if minutes is None or minutes > 360:
        return "HOLD", "마감 30분 전부터는 새로 안 삼"
    return "BUY", "급등 + 거래량 폭증"


def chase_check(sym: str, price: float, minutes: int | None, c: dict) -> tuple[bool, dict, str]:
    """🧗 고점 추격 필터: (살지, 수치, 설명). 1분봉을 못 받으면 막지 않고 '확인 불가'로 기록"""
    from . import shadow
    n = 400 if (minutes is not None and minutes > 190) else 200
    bs = shadow._bars(sym, n)
    if len(bs) < 5 or not price:
        return True, {}, "고점 확인 불가(1분봉 부족)"
    if minutes is not None and minutes >= 0:   # 정규장: 장 시작 이후 봉만
        t0 = time.time() - (minutes + 1) * 60
        day = [b for b in bs if shadow._ep(b["t"]) >= t0] or bs
    else:
        day = bs
    hi, lo = max([b["h"] for b in day] + [price]), min([b["l"] for b in day] + [price])
    last = bs[-5:]
    i5 = max(range(len(last)), key=lambda i: last[i]["h"])
    hi5 = max(last[i5]["h"], price)
    low_after = min([b["l"] for b in last[i5:]] + [price])
    m = {"from_high": round((price / hi - 1) * 100, 2),                       # 오늘 고점 대비(%)
         "range_pos": round((price - lo) / (hi - lo) * 100, 1) if hi > lo else 100.0,   # 오늘 저~고 범위 위치(%)
         "pullback5": round((1 - low_after / hi5) * 100, 2) if hi5 else 0.0,  # 직전 5분 고점에서 눌린 폭(%)
         "run5": round((price / last[0]["o"] - 1) * 100, 2) if last[0]["o"] else 0.0}   # 직전 5분 상승률(%)
    desc = f"고점 대비 {m['from_high']:+.2f}% · 범위 위치 {m['range_pos']:.0f}% · 5분 되돌림 {m['pullback5']:.2f}% · 5분 상승 {m['run5']:+.2f}%"
    if m["from_high"] > -float(c["chase_near_high_pct"]):
        if m["pullback5"] < float(c["chase_min_pullback_pct"]):
            return False, m, f"고점 추격: {desc} (고점 {c['chase_near_high_pct']:g}% 이내 · 되돌림 {c['chase_min_pullback_pct']:g}% 미만)"
        if m["run5"] >= float(c["chase_run5_max_pct"]):
            return False, m, f"고점 추격: {desc} (고점 근처 5분 급등 ≥ {c['chase_run5_max_pct']:g}%)"
    return True, m, desc


def falling5_check(sym: str, price: float) -> tuple[bool, float | None]:
    """📉 직전 5분 상승률이 마이너스(하락 중)면 안 삼: (살지, 5분 상승률). 1분봉 부족이면 막지 않음"""
    from . import shadow
    bs = shadow._bars(sym, 10)
    if len(bs) < 5 or not price or not bs[-5]["o"]:
        return True, None
    run5 = round((price / bs[-5]["o"] - 1) * 100, 2)   # chase_check 의 run5 와 같은 계산
    return run5 >= 0, run5


def _note_buy_skip(sym: str, why: str) -> None:
    """매수 안 한 이유를 활동 기록에 (같은 이유는 5분에 한 번)"""
    k = ("buy", sym, why[:40])
    if time.time() - _skip_seen.get(k, 0) < 300:
        return
    _skip_seen[k] = time.time()
    event("매수 안 함", f"{sym} — {why}", symbol=sym)


def rebound_check(sym: str, price: float, minutes: int | None, c: dict) -> tuple[bool, dict, str]:
    """🔄 반등 확인: 고점 후 눌림 → VWAP·MA5 위 회복 → 직전 1분봉 연속 상승일 때만 (살지, 수치, 설명). 확인 못 하면 안 삼"""
    from . import shadow
    n = 400 if (minutes is not None and minutes > 190) else 200
    bs = shadow._bars(sym, n)
    k = max(1, min(3, int(c.get("rebound_up_bars", 2))))
    if len(bs) < max(6, k + 1) or not price:
        return False, {}, "반등 확인 불가(1분봉 부족)"
    if minutes is not None and minutes >= 0:   # 정규장: 장 시작 이후 봉만
        t0 = time.time() - (minutes + 1) * 60
        day = [b for b in bs if shadow._ep(b["t"]) >= t0] or bs
    else:
        day = bs
    ih = max(range(len(day)), key=lambda i: day[i]["h"])   # 오늘 고점 봉
    hi = day[ih]["h"]
    low_after = min(b["l"] for b in day[ih:])               # 고점 이후 최저
    pv = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in day)
    vs = sum(b["v"] for b in day)
    vwap = pv / vs if vs else 0.0
    ma5 = sum(b["c"] for b in bs[-5:]) / 5
    last = bs[-(k + 1):]
    ups = sum(1 for i in range(1, len(last)) if last[i]["c"] > last[i]["o"] and last[i]["c"] > last[i - 1]["c"])
    m = {"rb_pullback": round((1 - low_after / hi) * 100, 2) if hi else 0.0,          # 고점 → 눌림 바닥(%)
         "rb_from_low": round((price / low_after - 1) * 100, 2) if low_after else 0.0,  # 눌림 바닥 대비 회복(%)
         "rb_vwap_gap": round((price / vwap - 1) * 100, 2) if vwap else 0.0,          # VWAP 대비(%)
         "rb_ma5_gap": round((price / ma5 - 1) * 100, 2) if ma5 else 0.0,             # MA5 대비(%)
         "rb_up_bars": ups}                                                            # 직전 연속 상승 봉 수
    desc = (f"고점 {hi:g}→바닥 {low_after:g} 눌림 {m['rb_pullback']:.2f}% · 바닥 대비 {m['rb_from_low']:+.2f}% · "
            f"VWAP {vwap:g}({m['rb_vwap_gap']:+.2f}%) · MA5 {ma5:g}({m['rb_ma5_gap']:+.2f}%) · 연속 상승 {ups}/{k}봉")
    pmin, pmax = float(c["rebound_pullback_pct"]), float(c["rebound_pullback_max_pct"])
    if m["rb_pullback"] < pmin:
        return False, m, f"반등 대기: 눌림 부족 ({desc} · 기준 {pmin:g}%↑)"
    if m["rb_pullback"] > pmax:
        return False, m, f"반등 아님: 너무 무너짐 ({desc} · 기준 {pmax:g}% 이하)"
    if not vwap or price <= vwap:
        return False, m, f"반등 대기: VWAP 아래 ({desc})"
    if price <= ma5:
        return False, m, f"반등 대기: MA5 아래 ({desc})"
    if ups < k:
        return False, m, f"반등 대기: 연속 상승 부족 ({desc})"
    return True, m, f"반등 확인: {desc}"


# 반등 근거(rb_)를 앞에 둬서 300자 자르기에도 남게
WHY_KEYS = (("rb_pullback", "고점 후 눌림 {:.2f}%"), ("rb_from_low", "바닥 대비 회복 {:+.2f}%"), ("rb_vwap_gap", "VWAP 대비 {:+.2f}%"),
            ("rb_ma5_gap", "MA5 대비 {:+.2f}%"), ("rb_up_bars", "연속 상승 {:.0f}봉"),
            ("change", "오늘 {:+.1f}%"), ("ratio", "거래량 {:.1f}배"), ("vol_ratio", "거래량 {:.1f}배"), ("pre", "프리장 거래량 {:.2f}"),
            ("score", "점수 {:.2f}"), ("from_high", "고점 대비 {:+.2f}%"), ("range_pos", "범위 위치 {:.0f}%"),
            ("pullback5", "5분 되돌림 {:.2f}%"), ("run5", "5분 상승 {:+.2f}%"), ("vtrend_r", "거래량 추세 {:.2f}배"))


def why_buy_text(feat: dict, est: dict | None, price: float, reason: str, ai_why: str = "") -> str:
    """📝 매수 근거를 수치로 (학습·복기용 · 절대 공란 아님)"""
    def _num(*ks):
        for k in ks:
            v = feat.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
        return None
    # 핵심 5개는 맨 앞에 고정 (없으면 '?'로 표시 · 300자 자르기에도 남게)
    chg, vr, r5, fh = _num("change"), _num("ratio", "vol_ratio"), _num("run5"), _num("from_high")
    ev = est.get("ev") if est else feat.get("ev")
    core = [f"급등 {chg:+.1f}%" if chg is not None else "급등 ?",
            f"거래량 {vr:.1f}배" if vr is not None else "거래량 ?",
            f"5분 추세 {r5:+.2f}%" if r5 is not None else "5분 추세 ?",
            f"고점 대비 {fh:+.2f}%" if fh is not None else "고점 대비 ?",
            f"기대값 {float(ev):+.2f}%" if isinstance(ev, (int, float)) else "기대값 ?"]
    parts = ["[" + " · ".join(core) + "]"]
    for k, fmt in WHY_KEYS:
        if k in ("change", "ratio", "vol_ratio", "run5", "from_high"):
            continue   # 위에 이미 넣음
        v = feat.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            parts.append(fmt.format(v))
    if est:
        parts.append(f"추정 승률 {float(est.get('p') or 0) * 100:.0f}% ({est.get('n', 0)}건)")
    known = {k for k, _ in WHY_KEYS} | {"p", "ev", "fx"}
    for k, v in feat.items():   # 스윙 조건 등 나머지 수치
        if k not in known and isinstance(v, (int, float)) and not isinstance(v, bool):
            parts.append(f"{k} {v:g}")
    parts.append(f"현재가 {price:g}")
    txt = " · ".join(parts) + f" | 규칙: {reason[:60]}"
    return (txt + (f" | AI: {ai_why[:80]}" if ai_why else ""))[:300]


def _note_hold(sym: str, avg: float, high: float, price: float, side: str, reason: str, c: dict) -> None:
    """보유 종목 기록: ① 고점 대비 기준이 바뀌면 (종목·이전→이후·이유) 기록 ② 5분마다 '지금 판단' 기록 (나중에 원인 찾기용)"""
    with _lock:
        s = state()
        new = trail_for(avg, high, c)
        old = s["trail_mode"].get(sym)
        changed = old is not None and abs(float(old) - new) > 1e-9
        if old is None or changed:
            s["trail_mode"][sym] = new
        now = time.time()
        log_due = side == "HOLD" and now - float(s["hold_log"].get(sym, 0)) >= 300
        if log_due:
            s["hold_log"][sym] = now
        _save(s)
    if changed:
        peak = (high / avg - 1) * 100
        event("기준 변경", f"{sym} 고점 대비 매도 기준 -{old:g}% → -{new:g}% (이유: 최고 수익 +{peak:.1f}% "
              f"{'≥' if new == c['trail_wide_pct'] else '<'} {c['trail_wide_after_pct']:g}%)", symbol=sym)
    if log_due:
        event("보유 확인", f"{sym} {reason}", symbol=sym)


def _filled(o: dict) -> bool:
    return o.get("status") == "paper_filled" or float(o.get("counted") or 0) > 0


def _partial_done(sym: str) -> bool:
    """'이미 나눠 팔았음' 확인 — 마지막 매수 뒤 실제 체결된 나눠 팔기 주문이 없으면 잘못 남은 표시라 지움 (재시작·예전 기록)"""
    with _lock:
        s = state()
        flag = s["partial"].get(sym)
        if not flag:
            return False
        if flag == "one":   # 1주라 못 나눈 경우 (주문 없음)
            return True
        bought = sold = False
        for o in s["orders"]:
            if o.get("symbol") != sym or not _filled(o):
                continue
            if o.get("side") == "BUY":
                bought, sold = True, False
            elif o.get("partial"):
                sold = True
        if not bought or sold:   # 매수 기록이 잘려서 모르면 그대로 둠
            return True
        s["partial"].pop(sym, None); _save(s)
    event("기록 정리", f"{sym} '이미 나눠 팔았음' 표시가 남아 있었지만 이번 매수 뒤 나눠 판 체결이 없어서 지움 → 나눠 팔기·익절 다시 작동", symbol=sym)
    return False


_skip_seen: dict[tuple, float] = {}


def _note_skip(sym: str, why: str) -> None:
    """규칙 기준(나눠 팔기·익절)을 넘었는데 안 팔고 보유할 때 이유 기록 (같은 이유는 5분에 한 번)"""
    k = (sym, why[:40])
    if time.time() - _skip_seen.get(k, 0) < 300:
        return
    _skip_seen[k] = time.time()
    event("보유 유지", f"{sym} 규칙 기준을 넘었지만 안 팖 — {why}", symbol=sym)


def _ai_ok(sym: str, side: str, reason: str, price: float) -> tuple[bool, str]:
    from . import providers as pv
    from . import team as tm
    prompt = (f"자동매매 규칙이 {sym} {('매수' if side == 'BUY' else '매도')} 신호를 냈어요. 이유: {reason}. 현재가 {price}.\n"
              "지금 내도 되는지 한 줄 이유와 함께 마지막 줄에 '판정: 찬성' 또는 '판정: 반대'. 매도는 특별한 이유가 없으면 찬성.")
    try:
        reply = pv.chat(tm.fast_spec(), "너는 리스크 관리자다. 짧게 답한다.", [{"role": "user", "content": prompt}], effort="low", timeout=60)
    except Exception as e:
        return side == "SELL", f"AI 검토 실패({type(e).__name__})"
    last = [l for l in reply.strip().splitlines() if "판정" in l][-1:] or [""]
    return ("찬성" in last[0] and "반대" not in last[0]), reply.strip().splitlines()[0][:160]


# ── 종목 준비·고르기 (백그라운드) ──
def _bg(name: str, fn) -> dict:
    m = cur_market()
    if _busy_m[m].is_set():
        return {"ok": True, "message": "이미 준비 중이에요"}

    def job():
        with in_market(m):
            _busy_m[m].set()
            try:
                fn()
            except Exception as e:
                event("오류", f"{name} 실패: {type(e).__name__}: {e}")
            finally:
                _busy_m[m].clear()
    threading.Thread(target=job, daemon=True, name=name).start()
    return {"ok": True, "message": f"{name} 시작 (몇 분 걸려요)"}


def _cap(v: float, *limits: float) -> float:
    """상한 적용 (0 이하 한도는 '제한 없음')"""
    for lim in limits:
        if lim and lim > 0:
            v = min(v, lim)
    return v


def _prep_cfg(c: dict) -> dict:
    """준비용: 종목당 예산(원)을 대략 계산해서 '1주도 못 사는 비싼 종목'을 미리 빼게 함"""
    c = dict(c)
    if c["budget_mode"] == "all_cash" and toss.enabled():
        try:
            fx = 1.0 if c["market"] == "KR" else usd_krw(c)
            cur = "KRW" if c["market"] == "KR" else "USD"
            cash = float((toss.buying_power(cur) or {}).get("cashBuyingPower") or 0)
            with _lock:
                held = sum(v["qty"] * v["avg"] for k, v in state()["bot_pos"].items() if _mkt(k) == c["market"])
            c["max_order_krw"] = _cap((cash + held) * fx / c["pick_count"], c["max_order_krw"], c["max_position_krw"])
            c["usd_krw"] = fx
        except toss.TossError:
            lim = _cap(1e12, c["max_order_krw"], c["max_position_krw"])
            c["max_order_krw"] = 0 if lim >= 1e12 else lim   # 현금을 모르면 한도만으로 (한도도 없으면 가격 상한 없이)
    return c


def prepare_now() -> dict:
    """장 전 준비: 급등주 기준값(단타) + 스윙(추세) 후보 — 전략에 맞게"""
    c = config()
    pc = _prep_cfg(c)
    mk = "미장" if c["market"] == "US" else "국장"

    def go():
        if c["strategy"] in ("surge", "auto"):
            b = picker.prepare_day(pc, log=_log_fn)
            event("준비", f"{mk} 급등주 기준값 {len(b.get('rows') or {})}개 준비 완료")
        if c["strategy"] in ("trend", "auto"):
            r = picker.scan({**c, "max_order_krw": pc["max_order_krw"], "usd_krw": pc.get("usd_krw", c["usd_krw"])}, log=_log_fn,
                            exclude=set(c["exclude"]))
            with _lock:
                s = state(); s["swing"] = r
                s.setdefault("swing_hist", []).append({"day": r["day"], "picks": [{"symbol": x["symbol"], "price": x["price"]} for x in r["picks"]]})
                s["swing_hist"] = [h for i, h in enumerate(s["swing_hist"]) if h["day"] not in [z["day"] for z in s["swing_hist"][i + 1:]]][-30:]
                _save(s)
            event("종목 선택", f"{mk} 스윙 " + r["note"] + (" — " + ", ".join(f"{p['name']}({p['symbol']})" for p in r["picks"][:6]) if r["picks"] else ""))
    return _bg("장 전 준비", go)


scan_now = prepare_now   # 예전 이름 호환


# ── 체결 알림 문구 (매매 로직과 무관, 보여주기만) ──
def _px_text(px: float, us: bool, fx: float) -> str:
    return f"${px:,.2f} (≈{px * fx:,.0f}원)" if us else f"{px:,.0f}원"


def _sell_kind(reason: str) -> str:
    """매도 이유 → 짧은 분류"""
    r = reason or ""
    for key, label in (("AI 판단", "🤖 AI 판단"), ("안전장치", "AI 익절 안전장치"), ("손절", "손절"), ("나눠 팔기", "나눠 익절"),
                       ("익절", "익절"), ("고점", "고점 대비 하락"), ("본전", "본전 정리"), ("마감", "마감 정리"),
                       ("장 끝", "마감 정리"), ("20일선", "20일선 이탈"), ("일 보유", "시간 초과"), ("보유 일수", "시간 초과"),
                       ("공시", "공시 악재")):
        if key in r:
            return label
    return r.split(":")[0][:30] or "사유 기록 없음"


def _fill_text(rec: dict, delta: float, fill_px: float, bp: dict, fx: float, gain: float, today: float) -> str:
    sym = rec["symbol"]; us = _mkt(sym) == "US"   # 시장 깃발은 event() 가 앞에 붙임
    first = (rec.get("reason") or "").splitlines()[0][:80] if rec.get("reason") else ""
    if rec["side"] == "BUY":
        style = "스윙" if (rec.get("features") or {}).get("style") == "swing" else "단타"
        return (f"🟢 매수 체결 {sym} {delta:g}주 @ {_px_text(fill_px, us, fx)} · {style}"
                + (f"\n이유: {first}" if first else ""))
    f = fx if us else 1
    cost = bp["avg"] * (1 + tl.side_cost(config(), _mkt(sym), "BUY") / 100) * delta * f   # 수수료 포함 산 금액(원)
    pct = gain / cost * 100 if cost > 0 else 0.0
    mark = "🔵 이익" if gain >= 0 else "🔴 손해"
    amt = f"{gain:+,.0f}원" + (f" (${gain / fx:+,.2f})" if us and fx else "")
    return (f"{mark} 매도 체결 {sym} {delta:g}주 @ {_px_text(fill_px, us, fx)} · 평단 {_px_text(bp['avg'], us, fx)}"
            f"\n손익(수수료·세금 뺌): {pct:+.2f}% · {amt}"
            f"\n사유: {_sell_kind(rec.get('reason', ''))}" + (f" — {first}" if first else "")
            + f"\n오늘 누적 실현: {today:+,.0f}원")


# ── 체결 확인 ──
def _reconcile(fx: float) -> None:
    with _lock:
        todo = [o for o in state()["orders"] if o.get("mode") == "live" and o.get("order_id") and not o.get("final")]
    for o in todo:
        try:
            r = toss.get_order(o["order_id"])
        except toss.TossError:
            continue
        ex = r.get("execution") or {}
        filled = float(ex.get("filledQuantity") or 0)
        fill_px = float(ex.get("averageFilledPrice") or o["price"])
        msg = stop_msg = ""
        with _lock:
            s = state()
            rec = next((x for x in s["orders"] if x.get("order_id") == o["order_id"]), None)
            if not rec:
                continue
            delta = filled - float(rec.get("counted", 0))   # 새로 체결된 수량만 (같은 체결 두 번 안 셈·안 보냄)
            if delta > 0 and rec.get("quote") and not rec.get("counted") and "손절" not in str(rec.get("reason", "")):   # 💱 실제 미끄러짐 (손절은 일부러 낮게 내서 제외)
                q_ = float(rec["quote"])
                sl_ = (fill_px / q_ - 1) * 100 if rec["side"] == "BUY" else (q_ / fill_px - 1) * 100
                s["slip_obs"] = (list(s.get("slip_obs") or []) + [round(sl_, 3)])[-60:]
            if delta > 0:
                bp = s["bot_pos"].get(rec["symbol"], {"qty": 0.0, "avg": 0.0})
                f = 1 if _mkt(rec["symbol"]) == "KR" else fx
                gain = 0.0
                if rec["side"] == "BUY":
                    tot = bp["qty"] + delta
                    s["bot_pos"][rec["symbol"]] = {"qty": tot, "avg": (bp["avg"] * bp["qty"] + fill_px * delta) / tot}
                    s["highs"][rec["symbol"]] = max(s["highs"].get(rec["symbol"], 0), fill_px)
                    s["style"][rec["symbol"]] = (rec.get("features") or {}).get("style", "surge")
                    s["buy_day"].setdefault(rec["symbol"], datetime.now(KST).strftime("%Y-%m-%d"))
                    s.setdefault("buy_t", {}).setdefault(rec["symbol"], time.time())   # ⏱️ 시간 손절용
                    tl.open_trade(rec["symbol"], delta, fill_px, rec.get("features"), "live", s["day"])
                else:
                    mk_ = _mkt(rec["symbol"]); c_ = config()   # 수수료·세금 포함 실제 손익
                    gain = (fill_px * (1 - tl.side_cost(c_, mk_, "SELL") / 100) - bp["avg"] * (1 + tl.side_cost(c_, mk_, "BUY") / 100)) * delta * f
                    _add_realized(s, rec["symbol"], gain, "live")
                    s["month"]["realized"] = float(s["month"].get("realized", 0)) + gain
                    left = bp["qty"] - delta
                    tl.on_sell(rec["symbol"], delta, fill_px, rec.get("reason", ""), "live", left <= 1e-9, {**config(), "_fx_now": fx})
                    try:   # 🧯 손절 체결가 기록 (오류 나도 체결 처리는 그대로)
                        stop_msg = _stop_fill(s, rec.get("client_order_id", ""), delta, fill_px)
                    except Exception:
                        stop_msg = ""
                    if left > 1e-9:
                        s["bot_pos"][rec["symbol"]] = {"qty": left, "avg": bp["avg"]}
                        if rec.get("partial"):
                            s["partial"][rec["symbol"]] = True
                    else:
                        s["bot_pos"].pop(rec["symbol"], None); s["highs"].pop(rec["symbol"], None); s["partial"].pop(rec["symbol"], None)
                        s["trail_mode"].pop(rec["symbol"], None); s["hold_log"].pop(rec["symbol"], None)
                        s["style"].pop(rec["symbol"], None); s["buy_day"].pop(rec["symbol"], None); s.setdefault("buy_t", {}).pop(rec["symbol"], None)
                rec["counted"] = filled
                try:   # 알림 문구 만들다 실패해도 체결 기록은 그대로
                    msg = _fill_text(rec, delta, fill_px, bp, fx, gain, float(s.get("realized_today", 0)))
                except Exception:
                    msg = ""
            st = r.get("status") or ""
            if st:
                rec["status"] = st.lower()
            if st in ("FILLED", "CANCELED", "REJECTED", "REPLACED"):
                rec["final"] = True
            _save(s)
        if delta > 0:
            event("체결", msg or f"{o['symbol']} {'매수' if o['side'] == 'BUY' else '매도'} {delta:g}주 @ {fill_px:g}", symbol=o["symbol"])
            if stop_msg:
                event("손절 기록", stop_msg, symbol=o["symbol"])
            if o["side"] != "BUY":
                _month_alert()   # 🎉 이번 달 +20% 첫 돌파 알림
            if o["side"] != "BUY":   # 🚀 급등 전략 확대 조건 도달 보고 (보고만, 비중은 안 바꿈)
                try:
                    gm = tl.hot_goal_check(_mkt(o["symbol"]))
                    if gm:
                        event("확대 조건", gm)
                except Exception as e:
                    _log_fn(f"🤖 [오류] 급등 전략 집계: {type(e).__name__}: {e}")
                _rebound_report(o["symbol"])


def _rebound_report(sym: str) -> None:
    """🔄 반등 진입 20건마다 비교 보고 (보고만 · 오류 나도 매매엔 영향 없음)"""
    try:
        rm = tl.rebound_goal_check(_mkt(sym))
        if rm:
            event("반등 비교", rm)
    except Exception as e:
        _log_fn(f"🤖 [오류] 반등 진입 집계: {type(e).__name__}: {e}")


# ── 🧯 손절 기록: 기준가·발동 시각·체결가 · 1%p 넘게 벌어지면 원인 · 손절 뒤 가격 추적 (기록만, 매매엔 영향 없음) ──
_last_seen: dict = {}   # 종목 → (직전 확인 가격, 시각(초), 장 날짜) · 메모리만
STOP_SLIP_PP = 0.3   # 손절선보다 이 %p 넘게 깊게 체결되면 '슬리피지'로 따로 표시
STOP_TRACK = ((30, "30분"), (120, "2시간"), (1440, "1일"), (4320, "3일"), (7200, "5일"))   # 손절 뒤 확인 시점(분)


def _stop_open(s: dict, sym: str, held: dict, price: float, reason: str, style: str, minutes, prev, c: dict,
               coid: str, fill: float | None, qty: float) -> None:
    """손절 발동 기록 (같은 종목 미체결 재주문이면 처음 발동 기록에 이어 붙임)"""
    log = s.setdefault("stop_log", [])
    now = time.time()
    old = next((r for r in reversed(log) if r.get("symbol") == sym and not r.get("fill_px") and now - float(r.get("ts", 0)) < 86400), None)
    if old:
        old.setdefault("ids", []).append(coid); old["retries"] = int(old.get("retries", 0)) + 1
        rec = old
    else:
        m_ = re.search(r"기준 -(\d+(?:\.\d+)?)%", reason or "")
        stop_pct = float(m_.group(1)) if m_ else float(c["swing_stop_pct"] if style == "swing" else c["stop_loss_pct"])
        avg = float(held.get("avg") or 0)
        if avg <= 0:
            return
        rec = {"symbol": sym, "style": style, "avg": avg, "stop_pct": stop_pct, "line": round(avg * (1 - stop_pct / 100), 4),
               "t": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"), "ts": now, "day": s.get("day"), "minutes": minutes,
               "trigger_px": float(price), "trigger_pnl": round((price / avg - 1) * 100, 2), "ids": [coid], "retries": 0,
               "prev_px": prev[0] if prev else None, "prev_ago_sec": round(now - prev[1]) if prev else None,
               "prev_same_day": bool(prev and prev[2] == s.get("day")), "track": {},
               "time_stop": "시간 손절" in (reason or ""), "live": fill is None}   # 슬리피지 집계용 (시간 손절 제외 · 실전/가상)
        log.append(rec)
    s["stop_log"] = log[-100:]
    if fill:
        _stop_fill(s, coid, qty, float(fill))


def _stop_cause(r: dict) -> str:
    """기준과 체결가 차이의 원인 (갭 · 확인 지연 · 호가 공백)"""
    out = []
    trig_dev = r["trigger_pnl"] + r["stop_pct"]   # 발동 순간 이미 기준 아래로 간 정도 (%p)
    slip = (r["fill_px"] / r["trigger_px"] - 1) * 100 if r.get("trigger_px") else 0.0
    if trig_dev <= -0.5:
        if not r.get("prev_same_day"):
            out.append(f"갭(오늘 첫 확인에서 이미 기준보다 {trig_dev:+.1f}%p)")
        elif r.get("prev_px") is not None and r["prev_px"] <= r["line"]:
            out.append(f"확인 지연(직전 확인 {r.get('prev_ago_sec')}초 전에도 이미 기준 아래였는데 주문이 늦음)")
        else:
            out.append(f"확인 지연(직전 확인 {r.get('prev_ago_sec')}초 사이 급락, 발동 때 기준보다 {trig_dev:+.1f}%p)")
    if int(r.get("retries", 0)) > 0:
        out.append(f"확인 지연(미체결로 {r['retries']}번 다시 주문)")
    if float(r.get("delay_sec") or 0) >= 30:   # 발동→체결까지 오래 걸림
        out.append(f"체결 지연(발동 뒤 {r['delay_sec']:.0f}초 만에 체결)")
    if slip <= -0.5:
        out.append(f"호가 공백(발동가 대비 체결 {slip:+.1f}%)")
    return " · ".join(out) or "작은 차이 누적"


def _stop_fill(s: dict, coid: str, qty: float, fill_px: float) -> str:
    """손절 체결가 반영 → 처음 체결일 때만 알림 문구 돌려줌"""
    r = next((x for x in reversed(s.get("stop_log") or []) if coid in (x.get("ids") or [])), None)
    if not r or qty <= 0:
        return ""
    first = not r.get("fill_px")
    q0 = float(r.get("fill_qty", 0))
    r["fill_px"] = round((float(r.get("fill_px") or 0) * q0 + fill_px * qty) / (q0 + qty), 4)   # 나눠 체결되면 평균
    r["fill_qty"] = q0 + qty
    r["fill_ts"] = r.get("fill_ts") or time.time()
    r["fill_t"] = r.get("fill_t") or datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    r["fill_pnl"] = round((r["fill_px"] / r["avg"] - 1) * 100, 2)
    r["gap_pp"] = round(r["fill_pnl"] + r["stop_pct"], 2)   # 기준 대비 차이 (%p, 음수 = 기준보다 더 잃음)
    # ⏱️ 지연·미끄러짐 나눠 기록: 발동 때 이미 밀린 몫(확인 지연·갭) + 발동가→체결가 몫(호가·체결 지연)
    r["delay_sec"] = round(float(r["fill_ts"]) - float(r.get("ts") or r["fill_ts"]), 1)
    r["trig_dev_pp"] = round(r["trigger_pnl"] + r["stop_pct"], 2)
    r["fill_slip_pct"] = round((r["fill_px"] / r["trigger_px"] - 1) * 100, 2) if r.get("trigger_px") else 0.0
    r["cause"] = _stop_cause(r) if r["gap_pp"] <= -1 else ""
    # 🔻 손절 슬리피지: 체결이 손절선보다 깊으면 따로 표시 (기록만)
    r["slip"] = (not r.get("time_stop")) and r["gap_pp"] <= -STOP_SLIP_PP
    try:   # 🧯 거래 기록(tradelearn)에도 붙임 (오류 나도 체결 처리는 그대로)
        tl.note_stop(r["symbol"], "live" if r.get("live", True) else "paper",
                     {k: r.get(k) for k in ("stop_pct", "line", "t", "trigger_px", "trigger_pnl", "prev_ago_sec", "retries",
                                            "fill_t", "fill_px", "fill_pnl", "gap_pp", "delay_sec", "trig_dev_pp",
                                            "fill_slip_pct", "cause", "slip", "time_stop")})
    except Exception:
        pass
    if not first:
        return ""
    r["low_px"] = r["high_px"] = r["fill_px"]
    return (f"🧯 손절 기록 {r['symbol']}: 기준 -{r['stop_pct']:g}%(기준가 {r['line']:g}) · 발동 {r['t'][5:]} @ {r['trigger_px']:g}"
            f"({r['trigger_pnl']:+.1f}%) · 체결 @ {r['fill_px']:g}({r['fill_pnl']:+.1f}%) · 기준 대비 {r['gap_pp']:+.1f}%p"
            f"\n지연·미끄러짐: 발동까지 {r['trig_dev_pp']:+.1f}%p · 발동→체결 {r['delay_sec']:.0f}초 {r['fill_slip_pct']:+.1f}%"
            + (" · 🔻 슬리피지" if r["slip"] else "")
            + (f"\n원인: {r['cause']}" if r["cause"] else ""))


def stop_slip_view(n: int = 20) -> dict:
    """🔻 손절 슬리피지 모아 보기 (손절선보다 깊게 체결된 손절만 · 화면·채팅용)"""
    rows = [r for r in (state().get("stop_log") or []) if r.get("fill_px") and not r.get("time_stop") and "gap_pp" in r]
    if not rows:
        return {"text": "", "rows": []}
    sl = [r for r in rows if float(r["gap_pp"]) <= -STOP_SLIP_PP]   # 예전 기록도 같은 기준으로 다시 셈
    if not sl:
        return {"text": f"🔻 손절 슬리피지: 최근 손절 {len(rows)}건 모두 손절선 근처에서 체결", "rows": []}
    avg = sum(float(r["gap_pp"]) for r in sl) / len(sl)
    worst = min(sl, key=lambda r: float(r["gap_pp"]))
    head = (f"🔻 손절 슬리피지: 손절 {len(rows)}건 중 {len(sl)}건이 손절선보다 깊게 체결 · 평균 {avg:+.2f}%p · "
            f"최악 {worst['symbol']} {worst['gap_pp']:+.1f}%p")
    out = [{"t": r.get("fill_t", "")[5:16], "symbol": r["symbol"], "live": r.get("live", True), "stop_pct": r["stop_pct"],
            "fill_pnl": r["fill_pnl"], "gap_pp": r["gap_pp"], "cause": r.get("cause") or ""} for r in sl[-n:]][::-1]
    lines = [head] + [f"- {x['t']} {x['symbol']}{'' if x['live'] else '(가상)'} 기준 -{x['stop_pct']:g}% → 체결 {x['fill_pnl']:+.1f}% "
                      f"({x['gap_pp']:+.1f}%p)" + (f" · {x['cause']}" if x["cause"] else "") for x in out[:5]]
    return {"text": "\n".join(lines), "rows": out, "n": len(sl), "total": len(rows), "avg_pp": round(avg, 2)}


def _stop_track(quotes: dict, minutes) -> list[str]:
    """손절 뒤 30분·2시간·1일·3일·5일 가격 변화 기록 (장중에만 갱신)"""
    if minutes is None:
        return []
    with _lock:
        s = state()
        todo = [r for r in s.get("stop_log") or [] if r.get("fill_px") and not r.get("track_done")]
    if not todo:
        return []
    need = [r["symbol"] for r in todo if r["symbol"] not in quotes]
    px = {k: v["price"] for k, v in quotes.items()}
    if need:
        try:
            px.update({k: v["price"] for k, v in toss.prices(list(dict.fromkeys(need))).items()})
        except toss.TossError:
            pass
    done_msgs = []
    with _lock:
        s = state()
        now = time.time()
        for r in s.get("stop_log") or []:
            p = px.get(r.get("symbol"))
            if not r.get("fill_px") or r.get("track_done") or not p:
                continue
            r["high_px"] = max(float(r.get("high_px") or p), p); r["low_px"] = min(float(r.get("low_px") or p), p)
            el = (now - float(r.get("fill_ts") or now)) / 60
            tr = r.setdefault("track", {})
            for mins_, label in STOP_TRACK:
                if el >= mins_ and label not in tr:   # 장 밖 시간이면 다음 장중 첫 가격으로 기록
                    tr[label] = {"px": p, "pct": round((p / r["fill_px"] - 1) * 100, 2)}
            if len(tr) >= len(STOP_TRACK):
                r["track_done"] = True
                up = (r["high_px"] / r["fill_px"] - 1) * 100
                last = tr["5일"]["pct"]
                r["verdict"] = ("손절 뒤 반등 — 손절이 일렀을 수 있음" if last >= r["stop_pct"] / 2 or up >= r["stop_pct"]
                                else "손절 뒤 추가 하락 — 손절 적정" if last <= -2 else "횡보 — 판단 보류")
                done_msgs.append(f"🧯 손절 뒤 추적 {r['symbol']} (손절 {r['fill_t'][5:16]} @ {r['fill_px']:g}): "
                                 + " · ".join(f"{k} {v['pct']:+.1f}%" for k, v in tr.items())
                                 + f" · 최고 {up:+.1f}% · 최저 {(r['low_px'] / r['fill_px'] - 1) * 100:+.1f}% → {r['verdict']}")
        _save(s)
    for m_ in done_msgs:
        event("손절 추적", m_)
    return done_msgs


def stop_log_text(n: int = 5) -> str:
    """최근 손절 기록 요약 (채팅 AI·복기용)"""
    rows = [r for r in (state().get("stop_log") or []) if r.get("fill_px")][-n:]
    if not rows:
        return ""
    out = ["🧯 최근 손절 기록 (기준 → 체결, 원인, 손절 뒤 변화):"]
    for r in rows:
        tr = " · ".join(f"{k} {v['pct']:+.1f}%" for k, v in (r.get("track") or {}).items())
        out.append(f"- {r['fill_t'][5:16]} {r['symbol']} 기준 -{r['stop_pct']:g}% → 체결 {r['fill_pnl']:+.1f}% ({r['gap_pp']:+.1f}%p)"
                   + (f" · 발동까지 {r['trig_dev_pp']:+.1f}%p · 발동→체결 {r['delay_sec']:.0f}초 {r['fill_slip_pct']:+.1f}%"
                      if "delay_sec" in r else "")   # 예전 기록엔 없음
                   + (f" · 원인: {r['cause']}" if r.get("cause") else "") + (f" · 뒤 {tr}" if tr else "")
                   + (f" · {r['verdict']}" if r.get("verdict") else ""))
    return "\n".join(out)


def _bot_order_ids() -> set[str]:
    with _lock:
        return {o["order_id"] for o in state()["orders"] if o.get("order_id")}


def _cancel_stale(minutes: float) -> None:
    """자동매매가 낸 미체결 주문 중 오래된 것 취소 (매도는 더 빨리: 다음 바퀴에 더 낮은 가격으로 다시 냄)"""
    now = datetime.now(timezone.utc)
    mine = _bot_order_ids()
    for o in [o for o in toss.open_orders() if o.get("orderId") in mine]:
        limit = minutes if o.get("side") != "SELL" else min(minutes, float(config().get("stop_resell_sec", 30)) / 60)   # 매도는 빨리 다시 냄
        try:
            t = datetime.fromisoformat(str(o.get("orderedAt")).replace("Z", "+00:00"))
            t = t if t.tzinfo else t.replace(tzinfo=KST)
        except ValueError:
            continue
        if now - t > timedelta(minutes=limit) and o.get("status") in ("PENDING", "PARTIAL_FILLED"):
            try:
                toss.cancel_order(o["orderId"])
                event("자동 취소", f"{o.get('symbol')} {limit:g}분 넘게 미체결 → 취소", symbol=o.get("symbol"))
            except toss.TossError as e:
                event("오류", f"취소 실패 {o.get('symbol')}: {e}")


# ── 한 바퀴 ──
def run_once(force: bool = False, exits_only: bool = False) -> dict:
    """규칙 한 번. force=True(화면의 '한 번 돌려보기')면 꺼져 있어도 신호만 계산.
    exits_only=True 면 들고 있는 종목의 매도 조건만 빠르게 확인 (10초마다)."""
    c = config()
    mode, market = c["mode"], c["market"]
    with _lock:
        s = state()
    if mode == "off" and not force:
        return {"ok": True, "message": "꺼져 있어요"}
    halted = s.get("halted_today", "")   # 정지돼도 '새로 사기'만 막고, 들고 있는 종목의 손절·정리는 계속
    if not toss.enabled():
        return {"ok": False, "message": "토스 키가 없어요 (🧰 도구)"}
    live, dry = mode == "live", mode == "off"
    cur = "KRW" if market == "KR" else "USD"
    results: list[str] = []
    try:
        fx = 1.0 if market == "KR" else usd_krw(c)
        if live:
            _reconcile(fx)
            _cancel_stale(1.5)
            h = toss.holdings()
            raw = {it["symbol"]: {"qty": float(it.get("quantity") or 0), "avg": float(it.get("averagePurchasePrice") or 0)} for it in h.get("items") or []}
            with _lock:
                s = state()
            if c["auto_pick"]:   # 자동매매가 산 수량만 (사장님이 직접 산 건 그대로)
                pos_live = {k: {"qty": min(v["qty"], raw[k]["qty"]), "avg": v["avg"] or raw[k]["avg"]} for k, v in s["bot_pos"].items() if k in raw}
            else:
                pos_live = {k: v for k, v in raw.items() if k in c["symbols"]}
            pending = {o.get("symbol") for o in toss.open_orders()}
            cash_local = 0.0 if exits_only else float((toss.buying_power(cur) or {}).get("cashBuyingPower") or 0)   # 매도만 볼 땐 현금 조회 생략
            # 🤝 가상으로 돌리는 AI 가 있으면 그 종목은 가상 장부에서 (같은 바퀴에 실전·가상이 섞여 돌아감)
            pos_paper = {k: {"qty": v["qty"], "avg": v["avg"]} for k, v in s["paper"]["positions"].items()}   # 가상 AI·검증 전 매수
            paper_cash = s["paper"]["cash"] / fx
        else:
            p = s["paper"]
            pos_live, pos_paper = {}, {k: {"qty": v["qty"], "avg": v["avg"]} for k, v in p["positions"].items()}
            pending, cash_local = set(), p["cash"] / fx
            paper_cash = cash_local
        pos_live = {k: v for k, v in pos_live.items() if _mkt(k) == market and v["qty"] > 0}
        pos_paper = {k: v for k, v in pos_paper.items() if _mkt(k) == market and v["qty"] > 0 and k not in pos_live}
        live_syms = set(pos_live)   # 🔒 실전 장부에 있는 종목만 실전으로 팜 (가상 종목은 절대 실전 주문 안 냄)
        pos = {**pos_paper, **pos_live}
        with _lock:   # 안 들고 있는 종목의 '이미 나눠 팔았음' 표시는 지움 (다음에 다시 사면 처음부터)
            s_p = state()
            gone = [k for k in s_p["partial"] if _mkt(k) == market and k not in pos]
            if gone:
                for k in gone:
                    s_p["partial"].pop(k, None)
                _save(s_p)
        held_syms = list(pos)
        quotes = toss.prices(held_syms) if held_syms else {}
        _val = lambda k, v: v["qty"] * quotes.get(k, {}).get("price", v["avg"]) * fx   # noqa: E731
        invested = sum(_val(k, v) for k, v in pos_live.items()) if live else sum(_val(k, v) for k, v in pos.items())   # 실전 장부 보유 가치
        invested_paper = sum(_val(k, v) for k, v in pos_paper.items())
        style_map = dict(s.get("style") or {})
        invested_by = {st: sum(_val(k, v) for k, v in pos.items() if (style_map.get(k) or "surge") == st) for st in LANES}
    except toss.TossError as e:
        event("오류", str(e))
        m_ = extras.toss_fail(market, e)
        if m_:
            event("오류", m_)
        return {"ok": False, "message": str(e)}
    m_ = extras.toss_ok(market)
    if m_:
        event("복구", m_)

    # 예산
    if c["budget_mode"] == "all_cash":
        budget = invested + cash_local * fx
        per_pos = _cap(budget / c["pick_count"], c["max_position_krw"])   # 종목당 = 현금÷종목 수, 단 '종목당 최대'를 넘지 않게
        caps = {"total": budget, "position": per_pos, "order": _cap(per_pos, c["max_order_krw"])}
        with _lock:   # 하루 손실 한도는 '그날 처음 예산' 기준으로 고정 (돈이 줄어도 한도가 같이 줄지 않게)
            s_b = state()
            if not s_b.get("day_budget") and budget > 0 and not exits_only:
                s_b["day_budget"] = budget; _save(s_b)
            day_budget = float(s_b.get("day_budget") or budget)
        loss_limit = day_budget * c["max_daily_loss_pct"] / 100
    else:
        caps = {"total": c["max_total_krw"], "position": _cap(c["max_total_krw"], c["max_position_krw"]),
                "order": _cap(c["max_total_krw"], c["max_order_krw"], c["max_position_krw"])}
        loss_limit = c["max_daily_loss_krw"]
    _unr = lambda k, v: (quotes.get(k, {}).get("price", v["avg"]) - v["avg"]) * v["qty"] * fx if v["avg"] else 0.0   # noqa: E731
    unreal_by = {st: sum(_unr(k, v) for k, v in pos.items() if (style_map.get(k) or "surge") == st) for st in LANES}
    unreal = sum(_unr(k, v) for k, v in (pos_live if live else pos).items())
    day_pl = float((s.get("realized_book") or {}).get("live" if live else "paper", s.get("realized_today", 0))) + unreal
    if live and not exits_only and not halted and day_pl <= -loss_limit and loss_limit > 0:
        halted = f"자동매매 오늘 손익 {day_pl:,.0f}원 — 한도 -{loss_limit:,.0f}원 초과"
        _halt_today(halted)   # 새로 사기만 멈추고, 이번 바퀴에도 들고 있는 종목 손절·정리는 계속
    # 🤝 AI 별 예산·손실 한도 (같은 장부를 쓰면 몫으로 나눔 · 가상 AI 는 가상 장부 전체)
    budget_paper = invested_paper + paper_cash * fx
    lane_caps: dict[str, dict] = {}
    halted_lane = dict(s.get("halted_lane") or {})
    # 🏟️ AI 리그 상위 AI 실전 몫 (실전 장부 예산의 arena_live_pct%)
    if live and set(s.get("arena_live_pos") or {}) - live_syms:   # 팔린 종목은 AI 리그 실전 목록에서 정리
        with _lock:
            s_c = state(); s_c["arena_live_pos"] = {k: v for k, v in (s_c.get("arena_live_pos") or {}).items() if k in live_syms}; _save(s_c)
        s["arena_live_pos"] = s_c["arena_live_pos"]
    arena_syms = set((s.get("arena_live_pos") or {}).keys())
    a_total = caps["total"] * float(c.get("arena_live_pct", 30)) / 100 if live else 0.0
    a_lp = _cap(a_total / max(1, c["pick_count"]), c["max_position_krw"])
    arena_caps = {"total": a_total, "position": a_lp, "order": _cap(a_lp, c["max_order_krw"])}
    invested_arena = sum(_val(k, v) for k, v in pos.items() if k in arena_syms and k in live_syms) if live else 0.0
    for st in LANES:
        book_total = (caps["total"] if lane_book(c, st) == "live" and live else budget_paper) * lane_share(c, st)
        lp = _cap(book_total / c["pick_count"], c["max_position_krw"])
        lane_caps[st] = {"total": book_total, "position": lp, "order": _cap(lp, c["max_order_krw"])}
        lane_pl = float((s.get("realized_lane") or {}).get(st, 0)) + unreal_by[st]
        if c["budget_mode"] == "all_cash":   # 그날 첫 예산 기준 (실전 장부) / 가상 장부 전체 기준
            base_ = day_budget if (lane_book(c, st) == "live" and live) else book_total
            lane_limit = base_ * c["max_daily_loss_pct"] / 100 * lane_share(c, st)
        else:
            lane_limit = loss_limit * lane_share(c, st)
        if not exits_only and lane_mode(c, st) != "off" and not halted_lane.get(st) and lane_limit > 0 and lane_pl <= -lane_limit:
            halted_lane[st] = f"{LANES[st]} 오늘 손익 {lane_pl:,.0f}원 — 한도 -{lane_limit:,.0f}원 초과"
            with _lock:
                s_h = state(); s_h.setdefault("halted_lane", {})[st] = halted_lane[st]; _save(s_h)
            event("정지", halted_lane[st] + " (이 AI 만 오늘 새로 안 삼)")
    month_key = datetime.now(KST).strftime("%Y-%m")
    with _lock:
        sm = state()
        if sm["month"].get("key") != month_key or not sm["month"].get("start"):
            sm["month"] = {"key": month_key, "start": caps["total"] - unreal, "realized": 0.0}
            _save(sm)
        month = sm["month"]
    month_pl = float(month.get("realized", 0)) + unreal
    month_pct = month_pl / month["start"] * 100 if month["start"] else 0
    target_hit = month_pct >= c["monthly_target_pct"]
    results.append(f"💼 예산 {caps['total']:,.0f}원 (종목당 {caps['position']:,.0f}원) · 보유 {invested:,.0f}원 · 오늘 손익 {day_pl:+,.0f}원")
    results.append("🤝 " + " · ".join(f"{LANES[st]} {dict(live='실전', paper='가상', off='쉼')[lane_mode(c, st)]} 몫 {lane_share(c, st) * 100:.0f}% "
                                      f"예산 {lane_caps[st]['total']:,.0f}원" + (" ⛔" if halted_lane.get(st) else "") for st in LANES))
    results.append(f"🎯 이번 달 {month_pct:+.1f}% / 목표 {c['monthly_target_pct']:g}%" + (" — 달성! 이번 달은 새로 안 사요" if target_hit and c["stop_at_target"] else ""))

    # 종목 후보: 단타(급등) + 스윙(추세)을 기대 수익률 순으로
    minutes = minutes_now(market, c)
    first = -330 if _ext(market, c) else 0
    strat = c["strategy"]
    cands: dict[str, dict] = {}   # 종목 → {style, features, reason, score}
    if exits_only:
        pass
    elif not c["auto_pick"]:
        for x in c["symbols"]:
            if x not in c["exclude"] and _mkt(x) == market:
                cands[x] = {"style": "swing" if strat == "trend" else "surge", "features": {}, "reason": "직접 고른 종목", "score": 0}
    elif halted:
        results.append("⛔ 오늘 정지 중 — 새로 사지 않고 들고 있는 종목만 관리 (" + halted + ")")
    else:
        slots = len(pos) < c["pick_count"]
        if strat in ("surge", "auto"):
            if not picker.base_ready(market):
                prepare_now()
                results.append("🔎 급등주 기준값 준비 중 (처음엔 몇 분~10여 분)")
            elif minutes is not None and first + 5 <= minutes <= 360 and slots:
                scan_cfg = {**c, "max_order_krw": caps["order"], "usd_krw": fx}
                r = picker.surge_scan(scan_cfg, log=_log_fn, exclude=set(pos) | set(c["exclude"]))
                # 초반 돌파 감지 추가
                br = picker.early_breakout_scan(scan_cfg, log=_log_fn, exclude=set(pos) | set(c["exclude"]))
                with _lock:
                    s2 = state(); s2["picks"] = r; s2["breakout"] = br; _save(s2)
                # 기존 급등주 후보
                for x in r["picks"]:
                    cands[x["symbol"]] = {"style": "surge", "features": {k: x.get(k) for k in ("change", "ratio", "pre", "minute", "score")},
                                          "reason": x["reason"], "score": x.get("score", 0), "info": x}
                # 초반 돌파 후보 (가상 매매 전용 기록, 2호 lane_surge_mode 기본값이 paper)
                for x in br["picks"]:
                    if x["symbol"] not in cands:   # 기존 급등주와 중복 제외
                        cands[x["symbol"]] = {"style": "surge", "features": {k: x.get(k) for k in ("change", "vol_ratio", "score")},
                                              "reason": "[초반돌파] " + x["reason"], "score": x.get("score", 0), "info": x}
                note_parts = ["🔎 단타 " + r["note"]]
                if br["picks"]:
                    note_parts.append(" | 초반 " + br["note"])
                results.append("".join(note_parts))
            elif minutes is None:
                results.append("🔎 장 시간이 아니에요")
        if strat in ("trend", "auto"):
            sw = s.get("swing") or {}
            if sw.get("day") != picker.session_date(market):
                if strat == "trend" or picker.base_ready(market):
                    prepare_now(); results.append("🔎 오늘의 스윙(추세) 종목 고르는 중")
            elif minutes is not None and minutes >= 10 and slots:   # 스윙은 정규장에서만 삼
                for x in sw.get("picks", []):
                    if x["symbol"] not in c["exclude"] and x["symbol"] not in pos and x["symbol"] not in cands:
                        cands[x["symbol"]] = {"style": "swing", "features": {k: x.get(k) for k in ("r20", "r60", "rsi", "score")},
                                              "reason": "[스윙] " + x["reason"], "score": x.get("score", 0), "info": x}
                if sw.get("picks"):
                    results.append(f"🔎 스윙 후보 {len(sw['picks'])}개 ({sw.get('note', '')})")
    if c.get("watchlist") and not exits_only:   # ⭐ 관심 종목
        try:
            cands.update(extras.watch_cands(market, c, set(cands) | set(pos), minutes))
        except Exception as e:
            results.append(f"⭐ 관심 종목 확인 오류: {type(e).__name__}")
    # 🎲 후보마다 기대 수익률 → 높은 순 (단타·스윙 상관없이)
    for sym, cd in cands.items():
        cd["features"].update(style=cd["style"], mkt=market)
        cd["est"] = tl.estimate(cd["features"], c)
        cd["reason"] += f" · 추정 승률 {cd['est']['p'] * 100:.0f}%({cd['est']['n']}건) · 기대 {cd['est']['ev']:+.2f}%"
    same: dict = {}   # 같은 '건수·기대값'으로 묶인 후보 → 묶음 기준을 기록에 남김
    for sym, cd in cands.items():
        same.setdefault((cd["style"], cd["est"]["n"], cd["est"]["ev"]), []).append(sym)
    for (sty, n_, ev_), syms in same.items():
        if len(syms) >= 2:
            gs = sorted({cands[x]["est"].get("group", "?") for x in syms})
            results.append(f"🧮 {len(syms)}종목이 같은 추정({n_}건 · 기대 {ev_:+.2f}%): " + ", ".join(syms[:6]) + (" 외" if len(syms) > 6 else "")
                           + " — 묶음 기준: " + " / ".join(gs[:3]) + " → 종목 고유값이 아니라 같은 조건 구간의 지난 거래로 계산됨")
    picked = sorted(cands, key=lambda k: (cands[k]["est"]["ev"], cands[k]["score"]), reverse=True)
    if c["news_on"] and not exits_only and news.enabled(market):   # 📰 상위 후보 공시 확인 (20분 보관) → 실전·그림자·AI 판단 공통
        for k in [k for k in picked if k not in pos][:8]:
            cands[k]["news"] = news.peek(k, market)   # 기다리지 않음 (처음엔 '확인 중', 다음 바퀴에 결과)
            if cands[k]["news"].get("ok"):
                cands[k]["features"]["news"] = "악재" if cands[k]["news"]["bad"] else "있음" if cands[k]["news"]["items"] else "없음"
    if not exits_only:   # 💱 상위 후보 호가 스프레드 (1분 보관 · 가상 체결을 현실에 가깝게)
        for k in [k for k in picked if k not in pos][:8]:
            try:
                cands[k]["spread_pct"] = extras.spread_pct(k)
            except Exception:
                pass
    if cands:
        with _lock:
            s2 = state(); s2["cands"] = {k: {"style": v["style"], "reason": v["reason"], "ev": v["est"]["ev"], "p": v["est"]["p"]} for k, v in cands.items()}; _save(s2)
    hot: dict = {}   # 🚀 30% 초과 급등 후보 (그림자 검증 조합 전용 · 실전은 안 삼)
    if c["shadow_on"] and not dry and not exits_only and not halted and c["auto_pick"] and strat in ("surge", "auto") \
            and picker.base_ready(market) and minutes is not None and first + 5 <= minutes <= 360:
        try:
            hot = shadow.hot_cands(c, market, fx, log=_log_fn)
        except Exception as e:
            results.append(f"🧪 급등 검증 후보 찾기 오류: {type(e).__name__}: {e}")

    symbols = list(dict.fromkeys(held_syms + picked))
    if not symbols:
        if not exits_only:   # 🧯 보유 종목이 없어도 손절 뒤 추적은 계속
            try:
                results += _stop_track(quotes, minutes)
            except Exception as e:
                results.append(f"🧯 손절 추적 오류: {type(e).__name__}: {e}")
        if c["shadow_on"] and not dry:   # 실전에 살 게 없어도 그림자 조합들의 매도는 계속
            try:
                shadow.step(c, market, cands, quotes, minutes, fx, exits_only, signal_surge, signal_swing, market_open,
                            lambda x: toss.daily(x, 60)[0], s["day"], hot=hot, pullback=_pb_today(s, market))
            except Exception as e:
                results.append(f"🧪 그림자 매매 오류: {type(e).__name__}: {e}")
        results += _arena_step(c, market, cands, quotes, minutes, s["day"], dry, exits_only, hot)
        _edge_step(market, quotes, minutes, s["day"], dry)
        return {"ok": True, "mode": mode, "results": results + ["지금 살 종목이 없어요"]}
    try:
        quotes.update(toss.prices([x for x in symbols if x not in quotes]) if [x for x in symbols if x not in quotes] else {})
    except toss.TossError as e:
        return {"ok": False, "message": str(e), "results": results}

    # 📚 장중 학습: 보유 종목 최대 상승·하락 갱신, 고른 종목(산 것·못 산 것)의 이후 움직임 기록
    mode_key = "live" if live else "paper"
    wday = f"{market}:{s['day']}"
    try:
        tl.mark({k: quotes[k]["price"] for k in pos if k in quotes}, mode_key)
        if not exits_only:
            wsyms = [x for x in tl.watch_symbols(wday) if x not in quotes]
            if wsyms:
                quotes.update(toss.prices(wsyms))
            tl.watch(wday, [cd["info"] for cd in cands.values() if cd.get("info") and cd["style"] == "surge"],
                     {k: v["price"] for k, v in quotes.items()})
    except (toss.TossError, OSError) as e:
        results.append(f"📚 학습 기록 일부 실패: {e}")
    if not exits_only:   # 📚 예전 스윙 일지의 빈 진입 조건 보충 (기록만, 매매엔 영향 없음)
        try:
            results += _backfill_swing(mode_key, market, fx)
        except Exception as e:
            results.append(f"📚 스윙 진입 조건 보충 실패: {type(e).__name__}: {e}")
    stop_why = tl.guard(wday, bool(minutes is not None and minutes < 0))

    for sym in symbols:
        tp_by_rule = None   # test 모드: 익절 규칙이 실제로 팔면 'AI 였으면?' 비교 시작
        q = quotes.get(sym)
        if not q:
            results.append(f"{sym}: 시세 없음"); continue
        price = q["price"]
        held = pos.get(sym, {"qty": 0.0, "avg": 0.0})
        prev_seen = _last_seen.get(sym)   # 🧯 직전 확인 가격 (손절 원인 판단용)
        if held["qty"] > 0:
            _last_seen[sym] = (price, time.time(), s["day"])
        with _lock:
            s3 = state()
            if held["qty"] > 0:
                s3["highs"][sym] = max(float(s3["highs"].get(sym, 0)), price); _save(s3)
            high = float(s3["highs"].get(sym, price))
            style = s3["style"].get(sym) if held["qty"] > 0 else (cands.get(sym) or {}).get("style")
            style = style or ("swing" if strat == "trend" else "surge")
            bought = s3["buy_day"].get(sym)
        if held["qty"] > 0 and style == "swing":
            try:
                closes, _, _ = toss.daily(sym, 60)
            except toss.TossError as e:
                results.append(f"{sym}: 차트 실패 {e}"); continue
            days = (datetime.now(KST).date() - datetime.strptime(bought, "%Y-%m-%d").date()).days if bought else 0
            side, reason = signal_swing(closes, price, held["qty"], held["avg"], high, days, c)
        elif held["qty"] > 0:
            p_done = _partial_done(sym)
            side, reason = signal_surge(price, held["qty"], held["avg"], high, minutes, False, c, partial_done=p_done)
            ts_why = time_stop(state().get("buy_t", {}).get(sym), held["avg"], max(high, price), price, c) if side == "HOLD" and not p_done else ""
            if ts_why:
                side, reason = "SELL", ts_why
            _note_hold(sym, held["avg"], max(high, price), price, side, reason, c)
            if side == "HOLD" and p_done and c.get("partial_tp_pct") and held["avg"] and \
                    (price / held["avg"] - 1) * 100 >= c["partial_tp_pct"]:
                _note_skip(sym, f"이미 나눠 팔았음 (평단 대비 {(price / held['avg'] - 1) * 100:+.1f}%) → 남은 물량은 +{c['take_profit_pct']:g}% 익절·고점 대비 하락 대기")
        elif sym in cands:
            side, reason = ("BUY", cands[sym]["reason"]) if (style == "swing" or (minutes is not None and minutes <= 360)) \
                else ("HOLD", "마감 30분 전부터는 새로 안 삼")
        else:
            side, reason = "HOLD", "선택 종목 아님"
        if held["qty"] > 0 and c["ai_take_profit"] != "off" and c["ai_decide"] and not dry:   # 🤖 AI 익절
            if side in ("SELL", "SELL_PART"):
                nt = _no_tp(c)
                if style == "swing":
                    side_r = signal_swing(closes, price, held["qty"], held["avg"], high, days, nt)[0]
                else:
                    side_r = signal_surge(price, held["qty"], held["avg"], high, minutes, False, nt,
                                          partial_done=_partial_done(sym))[0]
                if side_r == "HOLD":   # 이 매도는 익절 규칙이 낸 것
                    if c["ai_take_profit"] == "on" and _ai_room(c):
                        _note_skip(sym, f"AI 익절 모드(on)라 AI 가 판단 — 규칙이었으면: {reason[:60]}")
                        side, reason = "HOLD", f"🤖 익절은 AI 가 판단 (규칙이었으면: {reason[:60]})"
                    elif c["ai_take_profit"] == "test":
                        tp_by_rule = (dict(held), high, style, bought)
            if c["ai_take_profit"] == "on" and side == "HOLD":
                g_ = tp_guard(held["avg"], max(high, price), price, c)
                if g_:
                    side, reason = "SELL", g_
        if exits_only and side == "BUY":
            side = "HOLD"
        if side == "HOLD" and held["qty"] > 0 and c["ai_decide"] and not dry:   # 🤖 보유 중 AI 가 일찍 팔지 (따로 생각, 결과는 10초 확인 때도 반영)
            dec = _ai_peek(("hold", market, sym))
            if dec and dec != "PENDING" and dec[0]:
                side, reason = "SELL", f"🤖 AI 판단: {dec[1]}"
                event("AI 판단", f"{sym} 일찍 팔기 — {dec[1]}", symbol=sym, chart=dec[2])
                _ai_jobs.pop(("hold", market, sym), None)
            elif not exits_only and dec is None and _ai_hold_due(sym, c):
                _ai_async(("hold", market, sym), _ai_decide_hold, sym, dict(held), price, high, style, reason, c)
        if side == "HOLD":
            results.append(f"{sym}: {reason}"); continue
        if dry:
            results.append(f"{sym}: {('매수' if side == 'BUY' else '매도')} 신호 (꺼져 있어서 주문 안 함) — {reason}"); continue
        if not market_open(sym, side):
            if side != "BUY":
                _note_skip(sym, f"매매 시간이 아님 — {reason[:60]}")
            results.append(f"{sym}: 신호는 있지만 매매 시간이 아님 — {reason}"); continue
        if sym in pending:
            if side != "BUY":
                _note_skip(sym, f"미체결 주문이 있어서 대기 — {reason[:60]}")
            results.append(f"{sym}: 미체결 주문이 있어서 대기"); continue
        f = 1 if _mkt(sym) == "KR" else fx
        if side == "BUY":
            if halted:
                results.append(f"{sym}: ⛔ 오늘 정지 중이라 안 삼"); continue
            if lane_mode(c, style) == "off":
                results.append(f"{sym}: {LANES[style]} 는 쉬는 중이라 안 삼"); continue
            if halted_lane.get(style):
                results.append(f"{sym}: ⛔ {LANES[style]} 오늘 정지 중 — {halted_lane[style][:50]}"); continue
            arena_pick = None   # 🏟️ AI 리그 실전 자격 AI 가 '산다'고 했으면 실전으로
            if style == "surge" and live and c.get("arena_on") and c.get("arena_live") and not held["qty"]:
                try:
                    vs_, who_ = arena.live_vote(market, s["day"], sym, c)
                    if vs_ == "unseen":
                        vs_ = "pending" if arena.kick(c, market, sym, cands[sym], price, s["day"]) else "no"
                except Exception:
                    vs_, who_ = "no", None
                if vs_ == "pending":
                    results.append(f"{sym}: 🏟️ 리그 AI 들이 판단 중 (끝나면 바로 다시 확인)"); continue
                if vs_ == "yes":
                    arena_pick = who_
                elif who_ and who_.get("skipped"):   # 🧭 이 상황에선 대조군보다 못했던 AI 라 실전으로 안 감
                    _note_skip(sym, f"🧭 {who_['sit']} 에선 {', '.join(who_['skipped'])} 성적이 무작위 대조군보다 못해서 실전 안 감")
            buy_live = lane_live(c, style)   # 🔒 가상 AI 는 가상 장부로만
            est0 = (cands.get(sym) or {}).get("est") or {}
            if buy_live and c.get("live_needs_proof", True) and not (est0.get("n", 0) >= c["live_proof_n"] and est0.get("ev", 0) > 0):
                buy_live = False   # 🛡️ 아직 검증 안 된 조건 → 실전 돈 대신 가상 장부로 (기록은 똑같이 쌓임)
                proof_note = f"🛡️ 검증 전이라 가상으로 (비슷한 거래 {est0.get('n', 0)}건 · 기대 {est0.get('ev', 0):+.2f}% — 실전은 {c['live_proof_n']}건↑·플러스부터)"
            else:
                proof_note = ""
            if buy_live and not arena_pick and style == "surge":   # 🚧 지는 구간은 실전 대신 가상 (가상으로 계속 배워서 좋아지면 풀림)
                try:
                    blk_ = edge.bucket_block(market, (cands.get(sym) or {}).get("features") or {}, minutes)
                except Exception:
                    blk_ = ""
                if blk_:
                    buy_live, proof_note = False, blk_
            if buy_live and style == "swing" and not c.get("swing_live", False):
                buy_live, proof_note = False, "📉 스윙은 지금 가상으로만 (설정 '스윙 실전'을 켜면 실전)"
            elif buy_live and style == "surge" and c.get("live_quality", True):   # 🎯 강한 신호만 실전
                f0 = (cands.get(sym) or {}).get("features") or {}
                nw0 = (cands.get(sym) or {}).get("news") or {}
                strong = bool(f0.get("pre")) or float(f0.get("change") or 0) >= float(c["live_quality_change"])
                if not f0.get("pre") and float(f0.get("ratio") or 0) < float(c["live_quality_ratio"]):
                    strong = False
                if nw0.get("ok") and not nw0.get("items"):   # 공시를 확인했는데 이유가 없음
                    strong = False
                if not strong:
                    buy_live = False
                    proof_note = (f"🎯 강한 신호가 아니라 가상으로 (프리장 또는 +{c['live_quality_change']:g}%↑ · 정규장 거래량 {c['live_quality_ratio']:g}배↑ · 이유 있는 급등만 실전 — "
                                  f"지금 {float(f0.get('change') or 0):+.1f}% · {float(f0.get('ratio') or 0):.1f}배)")
            if arena_pick:   # 💰 검증 통과한 AI 리그 상위 AI 판단 → 실전 (현금·봇 장부·손실 한도·추격 확인은 그대로)
                buy_live, proof_note = True, (f"🏟️ AI 리그 {arena_pick['rank']}등 '{arena_pick['name']}' 판단"
                                              + (f"(+{', '.join(arena_pick['with'])})" if arena_pick.get('with') else "") + " → 실전")
            if style == "surge" and minutes is not None and minutes >= 0:
                chg0 = float(((cands.get(sym) or {}).get("features") or {}).get("change") or 0)
                if chg0 < float(c.get("surge_min_change_regular", 0) or 0):
                    results.append(f"{sym}: 📈 정규장 급등주는 +{c['surge_min_change_regular']:g}% 이상만 (지금 {chg0:+.1f}%)"); continue
                if c.get("surge_buy_cutoff_min") and minutes > float(c["surge_buy_cutoff_min"]):
                    results.append(f"{sym}: 🕒 정규장 {minutes}분째 — {c['surge_buy_cutoff_min']:g}분 뒤엔 급등주 새로 안 삼"); continue
            if minutes is not None and minutes < 0 and state().get("no_premarket_until", "") >= datetime.now(KST).strftime("%Y-%m-%d"):
                results.append(f"{sym}: 토스가 프리장 주문을 안 받아서 정규장부터 삼"); continue
            if target_hit and c["stop_at_target"]:
                results.append(f"{sym}: 이번 달 목표 달성 → 새로 안 삼"); continue
            if stop_why and style == "surge":
                results.append(f"{sym}: 📚 장중 학습 — {stop_why}"); continue
            try:   # 📉 시장 분위기 · 🕐 지는 시간대 · 🧺 업종 쏠림 · ⚠️ 토스 장애 (사는 것만 줄임)
                gate_mult, gate_why = extras.buy_gate(sym, style, minutes, market, list(pos), c)
            except Exception as e:
                gate_mult, gate_why = 1.0, ""
                results.append(f"{sym}: 관문 확인 오류 {type(e).__name__}")
            if gate_mult <= 0:
                _note_skip(sym, gate_why)
                results.append(f"{sym}: {gate_why}"); continue
            if style == "surge" and c.get("open_weak_skip", True) and minutes is not None and 0 <= minutes < float(c.get("open_weak_min", 30) or 0):
                try:   # 🌅 장 초반 + 지수 하락이면 급등주 건너뜀 (거래량 폭발 진입 손실 방지)
                    ich = extras.index_change(market)
                except Exception:
                    ich = None
                if ich is not None and ich <= -float(c.get("open_weak_idx_pct", 0.5)):
                    ow_why = f"🌅 정규장 {minutes}분째(초반 {c.get('open_weak_min', 30):g}분) · 지수 {ich:+.1f}% — 초반 약세장이라 급등주 안 삼"
                    _note_skip(sym, ow_why)
                    results.append(f"{sym}: {ow_why}"); continue
            if style == "swing":   # 📉 지수 급락일엔 스윙도 새로 안 삼
                crash_why = extras.crash_day(market, c)
                if crash_why:
                    _note_skip(sym, crash_why)
                    results.append(f"{sym}: {crash_why}"); continue
            try:   # 재진입 금지 기록이 없거나 깨져도 오류 없이 넘어감
                nr_all = state().get("no_rebuy")
                nr = str((nr_all if isinstance(nr_all, dict) else {}).get(sym) or "")
            except Exception:
                nr = ""
            if c.get("no_rebuy_after_loss", True) and nr and nr >= str(state().get("day") or ""):
                results.append(f"{sym}: 🔁 손해 보고 판 종목이라 {nr}까지 다시 안 삼"); continue
            if state()["orders_today"] >= c["max_orders_per_day"]:
                results.append(f"{sym}: 오늘 매수 횟수 한도({c['max_orders_per_day']}) 도달"); continue
            if sum(1 for k in pos if (style_map.get(k) or (cands.get(k) or {}).get("style") or "surge") == style) >= c["pick_count"]:
                results.append(f"{sym}: {LANES[style]} 가 이미 {c['pick_count']}종목 보유 중"); continue
            chase_m, chase_note = {}, ""
            if style == "surge" and c.get("falling5_block", True):   # 📉 직전 5분 하락 중이면 안 삼
                try:
                    ok_f, run5_f = falling5_check(sym, price)
                except Exception:
                    ok_f, run5_f = True, None   # 확인 오류면 막지 않음 (다른 관문이 판단)
                if not ok_f:
                    f_note = f"📉 직전 5분 하락 중 (5분 상승 {run5_f:+.2f}%) → 안 삼"
                    _note_buy_skip(sym, f_note)
                    results.append(f"{sym}: {f_note}"); continue
                if run5_f is not None:
                    chase_m = {"run5": run5_f}   # 📝 직전 5분 추세를 why_buy 에 (고점 필터 값이 있으면 그걸로 덮임)
            if style == "surge" and c.get("chase_filter", True):   # 🧗 고점 추격 필터 (진입 직후 역행 방지)
                try:
                    ok_c, ch_m, chase_note = chase_check(sym, price, minutes, c)
                except Exception as e:
                    ok_c, ch_m, chase_note = True, {}, f"고점 확인 오류({type(e).__name__})"
                chase_m = {**chase_m, **(ch_m or {})}   # 5분 추세 값 유지
                if not ok_c:
                    _note_skip(sym, f"🧗 {chase_note}")
                    results.append(f"{sym}: 🧗 {chase_note} → 안 삼"); continue
            if style == "surge" and c.get("rebound_entry", True):   # 🔄 반등 확인 후 진입 (확인 못 하면 안 삼)
                try:
                    ok_r, rb_m, rb_note = rebound_check(sym, price, minutes, c)
                except Exception as e:
                    ok_r, rb_m, rb_note = False, {}, f"반등 확인 오류({type(e).__name__})"
                if not ok_r:   # 대기는 자주 생겨서 이벤트 대신 결과 줄에만
                    results.append(f"{sym}: 🔄 {rb_note} → 안 삼"); continue
                chase_m = {**chase_m, **rb_m}   # 근거 수치를 why_buy·학습 기록에
                try:   # 🔄 근거 수치를 주문 이유에도
                    rb_txt = (f"눌림 {float(rb_m.get('rb_pullback', 0)):.2f}% · 연속 상승 {int(rb_m.get('rb_up_bars', 0))}봉 · "
                              f"바닥 대비 {float(rb_m.get('rb_from_low', 0)):+.2f}% · VWAP {float(rb_m.get('rb_vwap_gap', 0)):+.2f}% · "
                              f"MA5 {float(rb_m.get('rb_ma5_gap', 0)):+.2f}%")
                except Exception:
                    rb_txt = ""
                reason = f"{reason} · 🔄 반등 확인" + (f"({rb_txt})" if rb_txt else "")
            caps = arena_caps if arena_pick else lane_caps[style]
            buy_px = price * (1 + c["limit_slip_pct"] / 100) if style == "surge" else price
            est, size_note = cands[sym]["est"], ""
            nw = cands[sym].get("news")
            if nw and nw.get("pending"):
                results.append(f"{sym}: 📰 공시 확인 중 (끝나면 다음 바퀴에 판단)"); continue
            if nw and nw.get("ok") and nw["bad"]:   # 📰 악재 공시 → 안 삼
                msg = news.note(nw)
                if not _seen_news_block(sym):
                    event("공시 악재", f"{sym} 안 삼 — {msg}", symbol=sym)
                tl.flag_watch(wday, sym, news="악재")
                results.append(f"{sym}: {msg} → 안 삼"); continue
            cap_pos, cap_ord = caps["position"], caps["order"]
            if focus_ok(est, c) and caps["total"] * c["focus_max_pct"] / 100 > cap_pos:   # 🎯 확신할 때만 몰아넣기 ('종목당 최대'는 그대로 지킴)
                cap_pos = _cap(caps["total"] * c["focus_max_pct"] / 100, c["max_position_krw"])
                cap_ord = _cap(cap_pos, c["max_order_krw"])
                if cap_pos > caps["position"]:
                    size_note = f"🎯 확신 집중: 비슷한 거래 {est['n']}건 기대 {est['ev']:+.2f}% → 한도 {cap_pos:,.0f}원"
            budget = min(max(caps["order"], cap_ord), cap_pos - held["qty"] * price * f, caps["total"] - (invested_arena if arena_pick else invested_by[style]),
                         (cash_local if buy_live else paper_cash) * fx)
            if nw and nw.get("ok") and not nw["items"] and c["news_unknown_ratio"] < 1:   # 이유 모르는 급등 → 비중 축소
                budget *= c["news_unknown_ratio"]
                size_note = " · ".join(x for x in (size_note, f"📰 급등 이유 모름 → 금액 {c['news_unknown_ratio'] * 100:.0f}%") if x)
            if proof_note:
                size_note = " · ".join(x for x in (size_note, proof_note) if x)
            if arena_pick and float(arena_pick.get("size", 1)) < 1:   # 💰 비중 낮은 리그 AI 만 찬성 → 금액 줄임
                budget *= float(arena_pick["size"])
                size_note = " · ".join(x for x in (size_note, f"🏟️ AI 리그 비중 {float(arena_pick['size']) * 100:.0f}% 만큼만") if x)
            if gate_mult < 1:
                budget *= gate_mult
                size_note = " · ".join(x for x in (size_note, gate_why) if x)
            full_budget = budget
            if c["prob_sizing"]:   # 🎲 비슷한 조건의 지난 거래로 승률 추정 → 금액 조절
                ratio_, note_ = tl.size_ratio(est, c)
                explore = ratio_ > 0 and est["ev"] < 0   # 기대 마이너스인데 기록이 적어서 건너뛰지 않은 경우
                paper_surge = style == "surge" and not buy_live   # 🔬 2호 가상 매수는 위험 없음 → 하루 탐색 한도 없이 표본 쌓기 (실전 전환되면 다시 한도 적용)
                if explore:   # 🔬 탐색 매수도 최소 거래대금 기준 꼭 지킴
                    try:
                        ok_v, v_note = explore_value_ok(sym, price, fx, c)
                    except Exception as e:
                        ok_v, v_note = False, f"거래대금 확인 오류({type(e).__name__})"
                    if not ok_v:
                        results.append(f"{sym}: 🔬 탐색 매수 거래대금 미달 — {v_note} → 안 삼"); continue
                if explore and paper_surge:
                    ratio_ = float(c["min_size_ratio"])
                    note_ = f"🔬 탐색 매수(가상·한도 없음): 기대 {est['ev']:+.2f}% 지만 기록 {est['n']}건뿐이라 최소 금액({ratio_ * 100:.0f}%)으로 기록 쌓기"
                elif explore:
                    if int(state().get("explore_today", 0)) >= c["explore_per_day"]:
                        ratio_, note_ = 0.0, f"기대 수익 {est['ev']:+.2f}% (기록 {est['n']}건) · 오늘 탐색 매수 {c['explore_per_day']}번 다 씀 → 건너뜀"
                    else:
                        ratio_ = float(c["min_size_ratio"])
                        note_ = f"🔬 탐색 매수: 기대 {est['ev']:+.2f}% 지만 기록 {est['n']}건뿐이라 최소 금액({ratio_ * 100:.0f}%)으로 기록 쌓기"
                size_note = " · ".join(x for x in (size_note, note_) if x)
                if ratio_ <= 0:
                    event("확률 판단", f"{sym} 안 삼 — {size_note}", symbol=sym)
                    results.append(f"{sym}: 🎲 {size_note}"); continue
                full_budget, budget = budget, budget * ratio_
            qty = int(budget // (buy_px * f))
            if qty < 1 and c["prob_sizing"] and full_budget >= buy_px * f:   # 줄인 금액으론 1주가 안 되지만 한도 안에서 1주는 살 수 있으면 1주만
                qty = 1
                size_note += " (1주 미만이라 최소 1주)"
            if qty < 1:
                results.append(f"{sym}: 살 돈이 부족해요 (가능 {budget:,.0f}원, 1주 {buy_px * f:,.0f}원)"); continue
            chart_id, why = "", ""
            if c["ai_decide"] and not arena_pick:   # 🤖 AI 판단 (AI 리그 실전 AI 가 이미 판단했으면 건너뜀): 차트·조건·기대 수익률 보고 살지·얼마나 (따로 생각 → 손절 확인은 안 멈춤)
                dec = _ai_async(("buy", market, sym), _ai_decide_buy, sym, cands[sym], price, c)
                if dec == "PENDING":
                    results.append(f"{sym}: 🤖 AI 가 판단 중 (끝나면 바로 다시 확인)"); continue
                if dec:
                    ok, mult, why, chart_id = dec
                    tl.flag_watch(wday, sym, ai="찬성" if ok else "반대", why=why)
                    if not ok:
                        try:
                            edge.veto_add(market, sym, price, c, s["day"], "AI 판단")   # 🤖 막은 종목도 채점
                        except Exception:
                            pass
                        if edge.veto_mode(market) == "shrink":   # 막은 종목이 더 잘 됐던 기록 → 반대는 금액 줄이기로
                            ok, mult = True, 0.5
                            size_note += f" · 🤖 AI 반대({why[:40]})지만 그동안 막은 종목이 더 잘 돼서 금액 50%로"
                        else:
                            event("AI 판단", f"{sym} 안 삼 — {why}", symbol=sym, chart=chart_id)
                            results.append(f"{sym}: 🤖 AI 판단으로 안 삼 — {why}"); continue
                    qty2 = int(budget * mult // (buy_px * f))
                    qty = max(1, qty2) if qty2 < 1 and full_budget >= buy_px * f else qty2
                    if qty < 1:
                        results.append(f"{sym}: AI 가 줄인 금액으론 1주도 못 사요"); continue
                    size_note += f" · 🤖 AI 금액 {mult * 100:.0f}%"
            elif c["ai_chart_check"] and style == "surge" and not arena_pick:   # 📊 AI 가 1분봉 차트 그림을 직접 보고 판단
                ok, why, chart_id = _ai_chart_ok(sym, reason, price)
                tl.flag_watch(wday, sym, ai="찬성" if ok else "반대", why=why)
                if not ok:
                    try:
                        edge.veto_add(market, sym, price, c, s["day"], "AI 차트")
                    except Exception:
                        pass
                    if edge.veto_mode(market) == "shrink":
                        qty = max(1, qty // 2)
                        size_note += f" · 📊 AI 차트 반대({why[:40]})지만 그동안 막은 종목이 더 잘 돼서 수량 50%로"
                    else:
                        event("AI 차트 반대", f"{sym} 매수 보류 — {why}", symbol=sym, chart=chart_id)
                        results.append(f"{sym}: 📊 AI 가 차트 보고 반대 — {why}"); continue
            if c["ai_check"]:
                ok, why2 = _ai_ok(sym, side, reason, price)
                if not ok:
                    event("AI 반대", f"{sym} 매수 보류 — {why2}", symbol=sym); results.append(f"{sym}: AI 반대 — {why2}"); continue
            feat = dict(cands[sym]["features"])
            if market == "US":
                feat["fx"] = round(fx, 2)   # 환율 효과 계산용
            if style == "swing":   # 📚 스윙 진입 조건 기록 (기록용 · 이미 판단 끝난 뒤라 매수엔 영향 없음)
                try:
                    for k_, v_ in swing_feats(sym, price, fx).items():
                        if feat.get(k_) is None:
                            feat[k_] = v_
                except Exception as e:
                    results.append(f"{sym}: 📚 스윙 진입 조건 기록 실패 — {type(e).__name__}: {e}")
            try:   # 📈 진입 시점 거래량 추세 (기록용 · 매수엔 영향 없음)
                feat.update(vol_trend(sym))
            except Exception as e:
                results.append(f"{sym}: 📈 거래량 추세 기록 실패 — {type(e).__name__}: {e}")
            if c["ai_decide"] and why:
                feat["ai_decide"] = "산다"
            for k_, v_ in chase_m.items():   # 🧗 고점 위치·되돌림 수치도 학습 기록에
                if str(k_).startswith("rb_"):   # 🔄 반등 근거는 이번 진입 값으로 꼭 덮어씀
                    feat[k_] = v_
                else:
                    feat.setdefault(k_, v_)
            try:   # 📝 매수 근거를 수치로 반드시 기록
                feat["why_buy"] = why_buy_text(feat, est, price, reason, why)
            except Exception:
                feat["why_buy"] = f"현재가 {price:g} | 규칙: {reason[:60]}" + (f" | 기대값 {float(est['ev']):+.2f}%" if est and isinstance(est.get("ev"), (int, float)) else "")
            nnote = news.note(nw)
            if c["prob_sizing"] and est["ev"] < 0 and not (style == "surge" and not buy_live):   # 2호 가상 탐색은 1호 실전 한도를 안 깎음
                with _lock:
                    s_e = state(); s_e["explore_today"] = int(s_e.get("explore_today", 0)) + 1; _save(s_e)
            results.append(_execute(sym, "BUY", qty, buy_px, reason + (f" · {nnote}" if nnote else "") + (f" · 🎲 {size_note}" if size_note else "")
                                    + (f" · 🤖 AI: {why}" if why else ""), buy_live, fx, chart=chart_id, est=est, feat=feat, quote=price))
            invested_by[style] += qty * buy_px * f
            if arena_pick and buy_live:
                invested_arena += qty * buy_px * f
                with _lock:
                    s_a = state(); s_a.setdefault("arena_live_pos", {})[sym] = {"name": arena_pick["name"], "t": datetime.now(KST).strftime("%m-%d %H:%M")}; _save(s_a)
            if buy_live:
                invested += qty * buy_px * f; cash_local -= qty * buy_px; live_syms.add(sym)
            else:
                paper_cash -= qty * buy_px
            pos[sym] = {"qty": qty, "avg": buy_px}; style_map[sym] = style
        else:
            part = side == "SELL_PART"
            qty = int(held["qty"] * c["partial_tp_ratio"]) if part else int(held["qty"])
            if part and (qty < 1 or qty >= int(held["qty"])):   # 1주뿐이라 나눌 수 없으면 나눠 팔기 대신 계속 보유
                with _lock:
                    s4 = state(); s4["partial"][sym] = "one"; _save(s4)   # 'one' = 주문 없이 나눠 팔기만 끝난 것으로 표시
                _note_skip(sym, f"나눠 팔 수량이 안 됨(보유 {held['qty']:g}주) → 손절선은 본전으로, 남은 물량은 +{c['take_profit_pct']:g}% 익절 대기")
                results.append(f"{sym}: 나눠 팔 수량이 안 돼서(보유 {held['qty']:g}주) 계속 보유 — 손절선은 본전으로"); continue
            sell_live = live and sym in live_syms   # 🔒 실전 장부에 있는 종목만 실전 매도 (가상 AI 종목은 가상 장부)
            if sell_live and qty >= 1:   # 🔒 잠긴 봇 장부 기준 수량까지만 (사장님 주식 보호)
                try:
                    can = int(toss.bot_sellable(sym))
                except toss.TossError:
                    can = qty
                if can < qty:
                    results.append(f"{sym}: 봇 장부상 팔 수 있는 건 {can}주 → {can}주만")
                    qty = can
            if qty < 1:
                _note_skip(sym, f"봇 장부상 팔 수 있는 수량이 없음 — {reason[:60]}")
                results.append(f"{sym}: 팔 수량 없음"); continue
            sell_px = price * (1 - c["limit_slip_pct"] / 100) if style == "surge" else price
            if "손절" in reason:   # 🔻 손절·시간 손절은 확실히 팔리게 (지정가를 더 낮게 → 바로 체결)
                sell_px = price * (1 - float(c.get("stop_slip_pct", 2)) / 100)
            try:   # 매도 주문이 먼저 (기록 오류로 매도가 막히면 안 됨)
                results.append(_execute(sym, "SELL", qty, sell_px, reason, sell_live, fx, partial=part, quote=price))
            except Exception as e:
                with _lock:   # 실패하면 다음 바퀴에 AI 가 다시 판단
                    s_f = state(); s_f.setdefault("ai_hold_at", {}).pop(sym, None); _save(s_f)
                event("매도 오류", f"{sym} 매도 실패 — {type(e).__name__}: {e} (다음 바퀴에 다시 판단)", symbol=sym)
                results.append(f"{sym}: 매도 오류 — {type(e).__name__}: {e} (다음 바퀴에 다시 판단)"); continue
            if not part and _sell_kind(reason) == "손절":   # 🧯 손절 발동 기록 (오류 나도 매도엔 영향 없음)
                try:
                    stop_msg = ""
                    with _lock:
                        s_s = state()
                        o_ = next((o for o in reversed(s_s["orders"]) if o.get("symbol") == sym and o.get("side") == "SELL"), None)
                        if o_ and o_.get("status") != "rejected":
                            _stop_open(s_s, sym, held, price, reason, style, minutes, prev_seen, c, o_.get("client_order_id", ""),
                                       o_.get("fill") if not sell_live else None, qty)
                            if not sell_live:
                                r_ = next((r for r in reversed(s_s.get("stop_log") or []) if o_.get("client_order_id") in (r.get("ids") or [])), None)
                                if r_ and r_.get("fill_px"):
                                    stop_msg = (f"🧯 손절 기록 {sym}(가상): 기준 -{r_['stop_pct']:g}% · 발동 @ {r_['trigger_px']:g}({r_['trigger_pnl']:+.1f}%) · "
                                                f"체결 @ {r_['fill_px']:g}({r_['fill_pnl']:+.1f}%) · 기준 대비 {r_['gap_pp']:+.1f}%p"
                                                + (" · 🔻 슬리피지" if r_.get("slip") else "")
                                                + (f"\n원인: {r_['cause']}" if r_.get("cause") else ""))
                            _save(s_s)
                    if stop_msg:
                        event("손절 기록", stop_msg, symbol=sym)
                except Exception as e:
                    results.append(f"{sym}: 🧯 손절 기록 오류 — {type(e).__name__}: {e}")
            try:   # 재진입 금지·익절 비교 기록 (오류 나도 매도엔 영향 없음)
                if not part and c.get("no_rebuy_after_loss", True) and held.get("avg") and \
                        (price / held["avg"] - 1) * 100 < tl.rt_cost(c, market):   # 수수료 빼고 손해로 파는 거면 재진입 금지
                    until = str(state().get("day") or picker.session_date(market)) if style != "swing" else \
                        (datetime.now(KST) + timedelta(days=int(c.get("no_rebuy_swing_days", 3)))).strftime("%Y-%m-%d")
                    with _lock:
                        s_n = state(); nr_ = s_n.get("no_rebuy")
                        if not isinstance(nr_, dict):
                            nr_ = s_n["no_rebuy"] = {}
                        nr_[sym] = max(until, str(nr_.get(sym) or "")); _save(s_n)
                if tp_by_rule:
                    _ghost_open(sym, tp_by_rule[0], tp_by_rule[1], price, tp_by_rule[2], tp_by_rule[3])
            except Exception as e:
                results.append(f"{sym}: 매도 후 기록 오류 — {type(e).__name__}: {e}")
    if not exits_only:   # 🧯 손절 뒤 가격 추적 (기록만)
        try:
            results += _stop_track(quotes, minutes)
        except Exception as e:
            results.append(f"🧯 손절 추적 오류: {type(e).__name__}: {e}")
    if c["ai_take_profit"] == "test" and c["ai_decide"] and not dry:
        try:
            results += _ghost_step(c, market, minutes)
        except Exception as e:
            results.append(f"🤖 익절 비교 오류: {type(e).__name__}: {e}")
    if c["shadow_on"] and not dry:   # 🧪 같은 후보·시세로 규칙 조합들을 가상으로
        try:
            shadow.step(c, market, cands, quotes, minutes, fx, exits_only, signal_surge, signal_swing, market_open,
                        lambda x: toss.daily(x, 60)[0], s["day"], hot=hot, pullback=_pb_today(s, market))
        except Exception as e:
            results.append(f"🧪 그림자 매매 오류: {type(e).__name__}: {e}")
    results += _arena_step(c, market, cands, quotes, minutes, s["day"], dry, exits_only, hot)
    _edge_step(market, quotes, minutes, s["day"], dry)
    try:
        extras.gate_count(market, s["day"], results)   # 🚧 안 산 이유 하루 요약용
    except Exception:
        pass
    return {"ok": True, "mode": mode, "results": results}


def _pb_today(s: dict, market: str) -> list:
    """🧪 오늘 스윙 검색이 같이 고른 눌림 반등 후보 (그림자 검증 전용 · 실전은 안 삼)"""
    sw = s.get("swing") or {}
    return (sw.get("pullback") or []) if sw.get("day") == picker.session_date(market) and sw.get("market", market) == market else []


def _execute(sym: str, side: str, qty: int, price: float, reason: str, live: bool, fx: float, partial: bool = False,
             chart: str = "", est: dict | None = None, feat: dict | None = None, quote: float | None = None) -> str:
    coid = "cai-" + uuid.uuid4().hex[:20]
    px = toss.order_price(sym, price, side)
    label = f"{sym} {'매수' if side == 'BUY' else '매도'} {qty}주 @ {px}"
    rec = {"t": datetime.now(KST).strftime("%m-%d %H:%M:%S"), "symbol": sym, "side": side, "qty": qty, "price": float(px),
           "reason": reason, "mode": "live" if live else "paper", "client_order_id": coid, "partial": partial, "chart": chart,
           "quote": float(quote or price)}
    if side == "BUY":
        rec["features"] = dict(feat or {"style": "surge", "mkt": _mkt(sym)})
        if chart:
            rec["features"]["ai_chart"] = "찬성"
        if est:
            rec["features"].update(p=est["p"], ev=est["ev"])
    if live:
        try:
            r = toss.place_limit_order(sym, side, qty, price, coid)
            rec.update(order_id=r.get("orderId"), status="sent")
        except toss.TossError as e:
            rec.update(status="rejected", error=str(e))
            with _lock:
                s = state(); s["orders"].append(rec); _save(s)
            event("주문 거절", f"{label} — {e}", symbol=sym)
            mins_ = minutes_now(_mkt(sym))
            if side == "BUY" and mins_ is not None and mins_ < 0 and ("hours" in str(e).lower() or "시간" in str(e)):
                with _lock:   # 토스가 프리장 주문을 안 받음 → 일주일 동안 프리장 매수 안 함
                    s = state(); s["no_premarket_until"] = (datetime.now(KST) + timedelta(days=7)).strftime("%Y-%m-%d"); _save(s)
                event("실데이터", "토스가 프리장 주문을 받지 않아요 → 일주일 동안 프리장에는 사지 않고 정규장부터 살게요")
            return f"{sym}: 주문 거절 — {e}"
    else:
        f = 1 if _mkt(sym) == "KR" else fx
        p_ = tl.fill_px(extras.slip_cfg(config(), sym), float(quote or price), side, float(px))   # 현재가 + 미끄러짐, 지정가보다 나쁘게는 X
        rec["fill"] = round(p_, 4)
        with _lock:
            s = state(); p = s["paper"]
            cur = p["positions"].get(sym, {"qty": 0, "avg": 0})
            if side == "BUY":
                p["cash"] -= qty * p_ * f * (1 + tl.side_cost(config(), _mkt(sym), "BUY") / 100)
                tot = cur["qty"] + qty
                p["positions"][sym] = {"qty": tot, "avg": (cur["avg"] * cur["qty"] + p_ * qty) / tot}
                s["highs"][sym] = max(float(s["highs"].get(sym, 0)), p_)
                s["style"][sym] = rec["features"].get("style", "surge")
                s["buy_day"].setdefault(sym, datetime.now(KST).strftime("%Y-%m-%d"))
                s.setdefault("buy_t", {}).setdefault(sym, time.time())   # ⏱️ 시간 손절용
                tl.open_trade(sym, qty, p_, rec.get("features"), "paper", s["day"])
            else:
                c_ = config(); mk_ = _mkt(sym)   # 수수료·세금 포함
                p["cash"] += qty * p_ * f * (1 - tl.side_cost(c_, mk_, "SELL") / 100)
                gain = (p_ * (1 - tl.side_cost(c_, mk_, "SELL") / 100) - cur["avg"] * (1 + tl.side_cost(c_, mk_, "BUY") / 100)) * qty * f
                p["realized"] += gain; _add_realized(s, sym, gain, "paper")
                s["month"]["realized"] = float(s["month"].get("realized", 0)) + gain
                left = cur["qty"] - qty
                tl.on_sell(sym, qty, p_, reason, "paper", left <= 1e-9, {**config(), "_fx_now": fx})
                if left > 1e-9:
                    p["positions"][sym] = {"qty": left, "avg": cur["avg"]}
                    if partial:
                        s["partial"][sym] = True
                else:
                    p["positions"].pop(sym, None); s["highs"].pop(sym, None); s["partial"].pop(sym, None)
                    s["trail_mode"].pop(sym, None); s["hold_log"].pop(sym, None); s["style"].pop(sym, None); s["buy_day"].pop(sym, None)
                    s.setdefault("buy_t", {}).pop(sym, None)
            _save(s)
        rec["status"] = "paper_filled"
    with _lock:
        s = state(); s["orders"].append(rec)
        if side == "BUY":
            s["orders_today"] += 1
        _save(s)
    event("실전 주문" if live else "가상 주문", f"{label} — {reason}", symbol=sym, **({"chart": chart} if chart else {}))
    if not live and side != "BUY":   # 🔄 가상 매도 뒤 반등 진입 비교 보고
        _rebound_report(sym)
        _month_alert()   # 🎉 이번 달 +20% 첫 돌파 알림
    return f"{sym}: {'✅ 주문 보냄' if live else '📝 가상 체결'} {label} — {reason}"


def _halt_today(why: str) -> dict:
    with _lock:
        s = state(); s["halted_today"] = why; _save(s)
    event("정지", why)
    return {"ok": False, "message": "오늘은 정지: " + why}


def emergency_stop() -> dict:
    """긴급 정지: 꺼짐 + 자동매매가 낸 미체결 주문 전부 취소 (보유 주식은 그대로 — 팔지 않음)."""
    c = config(); was = c["mode"]
    c["mode"] = "off"
    c.pop("market", None)
    store.save(CONFIG, c)
    canceled, errors = 0, []
    if was == "live" and toss.enabled():
        mine: set[str] = set()
        for m in ("US", "KR"):   # 두 시장 모두
            with in_market(m):
                mine |= _bot_order_ids()
        try:
            for o in [o for o in toss.open_orders() if o.get("orderId") in mine]:
                try:
                    toss.cancel_order(o["orderId"]); canceled += 1
                except toss.TossError as e:
                    errors.append(str(e))
        except toss.TossError as e:
            errors.append(str(e))
    event("긴급 정지", f"꺼짐 (이전 {was}) · 미체결 취소 {canceled}건" + (f" · 실패: {errors[0]}" if errors else ""))
    return {"ok": not errors, "canceled": canceled,
            "message": f"자동매매를 껐어요. 미체결 {canceled}건 취소" + (" (일부 실패)" if errors else "") + ". 들고 있는 주식은 그대로예요 (토스 앱에서 직접 팔 수 있어요)"}


def reset_paper() -> dict:
    with _lock:
        s = state(); s["paper"] = {"cash": config()["paper_cash_krw"], "positions": {}, "realized": 0}; _save(s)
    event("가상 초기화", "가상 장부를 처음부터")
    return {"ok": True}


def _scorecard_safe(c: dict, s: dict) -> dict:
    try:
        fx = 1.0 if c["market"] == "KR" else usd_krw(c)
        sc = extras.scorecard(c["market"], "live" if c["mode"] == "live" else "paper", fx,
                              float((s.get("month") or {}).get("start") or 0), s.get("orders", []))
        sc["text"] = extras.scorecard_text(c["market"], sc)
        return sc
    except Exception as e:
        return {"text": f"성적표 계산 오류: {type(e).__name__}"}


def overview() -> dict:
    c, s = config(), state()
    return {"config": c, "toss": toss.enabled(), "day": s["day"], "orders_today": s["orders_today"],
            "halted_today": s.get("halted_today", ""), "halt_release": halt_release_kst() if s.get("halted_today") else "", "paper": s["paper"], "orders": s["orders"][-60:][::-1],
            "events": s["events"][-100:][::-1], "running": bool(_thread and _thread.is_alive()),
            "picks": s.get("picks") or {}, "scanning": _busy().is_set(), "bot_pos": s.get("bot_pos", {}),
            "realized_today": s.get("realized_today", 0), "base_ready": picker.base_ready(c["market"]), "month": s.get("month", {}),
            "session_minutes": minutes_now(c["market"], c),
            "local_time": picker.mnow(c["market"]).strftime("%H:%M"), "progress": dict(picker.PROGRESS), "next_open": _next_open(c),
            "learn": tl.overview(s["day"], "live" if c["mode"] == "live" else "paper", c["market"]),
            "market": c["market"], "markets": c["markets"], "cands": s.get("cands", {}), "swing": s.get("swing") or {}, "style": s.get("style", {}),
            "brief": {m: _brief(m) for m in c["markets"]}, "lab": lab_overview(), "preflight": s.get("preflight") or {},
            "scorecard": _scorecard_safe(c, s), "gates": extras.gate_text(c["market"], s["day"]), "edge": _edge_text(c["market"]), "arena": _arena_ov(c["market"]), "arena_replay": _replay_text(c["market"]), "rules": extras.rule_history(8),
            "lanes": lanes_overview(c["market"]), "lessons": tl.lessons(c["market"], 10), "notes_core": tl.core(c["market"]),"halted_lane": s.get("halted_lane") or {},
            "stop_slip": _stop_slip_safe()}


def _stop_slip_safe() -> dict:
    try:   # 🔻 손절 슬리피지 (오류 나도 화면은 그대로)
        return stop_slip_view()
    except Exception as e:
        return {"text": f"🔻 손절 슬리피지 계산 오류: {type(e).__name__}", "rows": []}


def _brief(m: str) -> dict:
    """시장별 한 줄 상태 (화면 위쪽)"""
    with in_market(m):
        c, s = config(), state()
        pos = s["bot_pos"] if c["mode"] != "paper" else s["paper"]["positions"]
        return {"held": len(pos), "orders_today": s["orders_today"], "halted": s.get("halted_today", ""),
                "realized_today": round(float(s.get("realized_today", 0))), "minutes": minutes_now(m, c),
                "local_time": picker.mnow(m).strftime("%H:%M"), "next_open": _next_open(c)}


def _review_job(day: str, c: dict, mode_key: str) -> None:
    try:
        r = tl.daily_review(day, c, mode_key)
        with _lock:
            s = state(); s["reviewed_day"] = day; _save(s)
        t = r["today"]
        from . import statwin as sw   # 📏 20건 미만이면 표본 부족 · 누적은 기준 이후만
        event("복기", f"{day} 거래 {t.get('n', 0)}건" + (f" · {sw.win_text(t['n'], t['win_rate'])} · 평균 {t['avg']:+.2f}%" if t.get("n") else "")
              + (f" · 누적 {r['since']['text']}" if (r.get("since") or {}).get("text") else "")
              + (f" · 조정 제안 {len(r['proposals'])}개" if r.get("proposals") else ""))
        if config()["auto_tune"]:
            auto_adjust(r, mode_key)
    except Exception as e:
        event("오류", f"복기 실패: {type(e).__name__}: {e}")
    try:   # 🤝 두 AI 교차 복기 → 공유 노트
        if config()["lane_cross_review"]:
            _cross_review(day, c["market"])
    except Exception as e:
        event("오류", f"교차 복기 실패: {type(e).__name__}: {e}")
    try:   # 🤝 2호 가상→실전 자동 · 연속 손실이면 가상으로 · 예산 몫 조정
        lane_auto(c["market"])
    except Exception as e:
        event("오류", f"AI 자동 조정 실패: {type(e).__name__}: {e}")
    try:   # 🎯 판단 질 점검: AI 반대 검증 · 지는 구간 차단 · 승률 추정 보정
        for m_ in edge.review(c["market"]):
            event("판단 점검", m_)
    except Exception as e:
        event("오류", f"판단 점검 실패: {type(e).__name__}: {e}")
    try:   # 📼 AI 리그 과거 장 복습 (뒤에서 · 장 닫힌 뒤라 매매 영향 없음)
        c_r = config()
        if c_r.get("arena_on") and c_r.get("arena_replay", True):
            from . import arena_replay
            arena_replay.start(c_r, c["market"], _log_fn)
    except Exception as e:
        event("오류", f"과거 장 복습 시작 실패: {type(e).__name__}: {e}")
    try:   # 📉 1호 스윙: 가상으로 30일·30건↑ 평균 마이너스면 쉬게 (2호·AI 리그에 집중)
        c_s = config()
        if c_s.get("swing_auto_off", True) and lane_mode(c_s, "swing") == "paper":
            ls_ = lane_stats(c["market"], "swing", 30)
            if ls_.get("n", 0) >= 30 and float(ls_.get("avg", 0)) < 0:
                if save_config({"lane_swing_mode": "off"}, _by_auto=True).get("ok"):
                    event("판단 점검", f"📉 1호 스윙(가상) 최근 30일 {ls_['n']}건 평균 {ls_['avg']:+.2f}% → 쉬게 했어요 (새로 안 삼 · 보유는 계속 관리 · 화면에서 다시 켤 수 있어요)")
    except Exception as e:
        event("오류", f"스윙 점검 실패: {type(e).__name__}: {e}")
    try:   # 🏟️ AI 리그 실전 자격 AI 성적이 떨어졌으면 바로 내림
        rm_ = arena.recheck(config(), c["market"])
        if rm_:
            event("AI 리그", rm_)
    except Exception as e:
        event("오류", f"AI 리그 자격 확인 실패: {type(e).__name__}: {e}")
    c2 = config()
    try:
        if c2["shadow_on"] and c2["auto_promote"] and c2["mode"] != "off":
            msg = shadow.maybe_promote(c2, c2["market"], _apply_params)
            if msg:
                event("규칙 교체", msg)
    except Exception as e:
        event("오류", f"규칙 교체 실패: {type(e).__name__}: {e}")
    try:   # 🧪 20건↑ 조합 중 하위 25% 정리 → 새 조합으로 채워 20개 안팎 유지
        if c2["shadow_on"]:
            pm = shadow.prune(config(), c2["market"])
            if pm:
                _log_fn(f"🧪 그림자 조합 정리: {pm}")
    except Exception as e:
        event("오류", f"그림자 조합 정리 실패: {type(e).__name__}: {e}")
    try:   # 🗓️ 내일 계획 · 📊 주간 성적표 · 🧾 연말 세금 알림
        fx_ = 1.0 if c2["market"] == "KR" else usd_krw(c2)
        with _lock:
            s_r = state()
            extras.after_review(c2["market"], mode_key, fx_, s_r, c2)
            _save(s_r)
    except Exception as e:
        event("오류", f"장 마감 알림 실패: {type(e).__name__}: {e}")
    try:   # ↩️ 자동 규칙 변경 효과 확인 (나빠졌으면 그 변경만 되돌림) · 💱 가상 체결 미끄러짐 보정
        m_ = extras.rule_check(_log_fn)
        if m_:
            event("자동 조정", m_)
        with _lock:
            m2 = extras.calibrate_slip(config(), state(), _log_fn)
        if m2:
            event("자동 조정", m2)
    except Exception as e:
        event("오류", f"규칙 변경 확인 실패: {type(e).__name__}: {e}")
    try:   # 📋 제안 현황 하루 요약 (대기·적용·바꾼 뒤 성적 · 텔레그램만, 읽기 전용)
        if c2.get("daily_digest", True):
            from . import digest
            digest.send(c2["market"])
    except Exception as e:
        event("오류", f"제안 현황 요약 실패: {type(e).__name__}: {e}")
    try:   # 🛠️ 매매 코드 자동 개선 효과 확인 → 나빠졌으면 되돌림
        if c2.get("improve_effect_check", True):
            msg = extras.improve_effect(_log_fn, _restart_fn[0])
            if msg:
                event("자동 조정", msg)
    except Exception as e:
        event("오류", f"자동 개선 효과 확인 실패: {type(e).__name__}: {e}")
    if c2["backtest_on"] and toss.enabled():
        backtest_now(wait=True)
    try:   # 🧾 그림자·백테스트·반등 비교 누적 요약 (하루 한 번 · 기록·텔레그램만)
        _verify_daily(day)
    except Exception as e:
        event("오류", f"검증 요약 실패: {type(e).__name__}: {e}")


def verify_text(market: str) -> str:
    """그림자 매매·백테스트·반등 확인 진입 비교의 누적 건수·승률·평균 손익 (읽기만)"""
    from . import statwin as sw   # 📏 승률·평균은 기준 이후 거래만 (이전은 학습용 표본으로 따로)
    f = lambda x, n="n_all", w="win_all", a="avg_all": (x["ss"]["text"] if isinstance(x.get("ss"), dict) else  # noqa: E731
                                                       f"{x.get(n, 0)}건 · {sw.win_text(int(x.get(n, 0)), x.get(w, 0))} · 평균 {float(x.get(a, 0)):+.2f}%")
    lines = [f"🧾 검증 누적 요약 ({'미장' if market == 'US' else '국장'} · 수수료 뺀 값 · 📏 집계 기준 {sw.since()} 이후)"]
    try:
        sh = shadow.totals(market)
        a = sh["acct"]
        lines.append(f"🧪 그림자 '현재' 규칙: {f(sh['base'])} · 최대 손실(전체) {sh['base'].get('worst', 0):+.2f}%")
        lines.append(f"🧪 그림자 조합 전체({sh['variants']}개): {f(sh['all'])}")
        lines.append(f"📉 급락반등 진입(가상): {f(sh['flash_rebound'])}")
        bn = ((sh["base"].get("ss") or {}).get("n", 0))
        lines.append(f"💰 100만 원 가상 계좌({a['start'][:10]}~): 전체 {a['n']}건(기준 이후 {bn}건) · 확정 {a['realized_pct']:+.2f}% · 평가 포함 {a['total_pct']:+.2f}%")
    except Exception as e:
        lines.append(f"🧪 그림자: 집계 실패 ({type(e).__name__})")
    b = bt.load(market)
    if b.get("baseline"):
        lines.append(f"📼 최근 백테스트({b.get('day', '')}) 현재 규칙: "
                     + ((b.get("baseline_since") or {}).get("text") or f(b['baseline'], 'n', 'win', 'avg')))
    else:
        lines.append("📼 백테스트: " + (b.get("note") or "기록 없음"))
    rp = (b.get("replay") or {}).get("all") or {}
    if rp:
        rps = (b.get("replay") or {}).get("since") or {}
        if (rps.get("rebound") or {}).get("text"):
            lines.append(f"🔄 과거 복기 반등 확인 {rps['rebound']['text']} / 기존 {(rps.get('plain') or {}).get('text', '-')}"
                         f" · (전체 기간 판정) {(b.get('replay') or {}).get('verdict', '')}")
        else:
            lines.append(f"🔄 과거 복기 반등 확인 {f(rp.get('rebound') or {}, 'n', 'win', 'avg')} / 기존 {f(rp.get('plain') or {}, 'n', 'win', 'avg')}"
                         f" · {(b.get('replay') or {}).get('verdict', '')}")
    try:
        rs = tl.rebound_stats(market)
        r, p = rs["rebound"], rs["plain"]
        ss = rs.get("since") or {}
        lines.append(f"🔄 실제 2호 진입 비교: 반등 확인 {ss['rebound']['text']} (전체 {r['n']}/{rs['goal_n']}건) / 기존 {ss['plain']['text']}")
    except Exception as e:
        lines.append(f"🔄 반등 비교: 집계 실패 ({type(e).__name__})")
    try:   # 🤝 1호·2호 (기준 이후)
        with tl._lock:
            ts_ = [t for t in tl._load()["trades"] if t["closed"] and tl._tm(t) == market]
        lines.append("🤝 " + " / ".join(f"{LANES[k]} {sw.view([t for t in ts_ if tl._ts(t) == k])['text']}" for k in LANES))
    except Exception:
        pass
    lines.append("※ 기록·보고만 해요 — 실전 전환은 사장님이 화면에서 직접 정해 주세요")
    return "\n".join(lines)


def _verify_daily(day: str) -> None:
    """하루 한 번만: 요약을 상태에 저장(채팅 자료) + 기록·텔레그램"""
    with _lock:
        s = state()
        if (s.get("verify_daily") or {}).get("day") == day:
            return
    text = verify_text(cur_market())
    with _lock:
        s = state(); s["verify_daily"] = {"day": day, "t": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "text": text}; _save(s)
    event("검증 요약", text)


def review_now(wait: bool = False) -> dict:
    """장 마감 후 복기 (자동 또는 화면 버튼)"""
    c = config()
    mode_key = "live" if c["mode"] == "live" else "paper"
    day = state()["day"]
    m = cur_market()

    def job():
        with in_market(m):
            _review_job(day, c, mode_key)

    if wait:
        job(); return {"ok": True}
    threading.Thread(target=job, daemon=True, name="review").start()
    return {"ok": True, "message": "복기 시작 (AI 요약까지 1분쯤)"}


def _next_open(c: dict) -> str:
    """다음에 종목 찾기를 시작하는 한국 시각 (예: '오늘 18:05')"""
    market = c["market"]
    first = -330 if _ext(market, c) else 0
    now = datetime.now(KST)
    for step in range(0, 60 * 24 * 4, 5):   # 5분 단위로 최대 4일 앞까지
        t = now + timedelta(minutes=step)
        m = picker.session_minutes(market, t, extended=_ext(market, c))
        if m is not None and first + 5 <= m <= 360:
            day = "오늘" if t.date() == now.date() else ("내일" if t.date() == (now + timedelta(days=1)).date() else t.strftime("%m/%d"))
            return "지금" if step == 0 else f"{day} {t:%H:%M}"
    return ""


# ── 1시간 장 상황 요약 (텔레그램, 읽기 전용) ──
_hourly_sent = [""]                       # 마지막으로 보낸 시각 (KST 'YYYY-mm-dd HH')
_save_fails: list[tuple[float, str]] = []  # 저장 실패 기록 (요약용)


def _ago_sec(t: str) -> float | None:
    """'mm-dd HH:MM:SS'(KST) → 지금부터 몇 초 전 (연도는 올해, 미래면 작년)"""
    try:
        now = datetime.now(KST)
        dt = datetime.strptime(f"{now.year}-{t}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
        if dt > now + timedelta(minutes=1):
            dt = dt.replace(year=now.year - 1)
        return (now - dt).total_seconds()
    except (ValueError, TypeError):
        return None


def _in_session(m: str) -> bool:
    """정규장(미장은 프리장 포함) 중이고 오늘 휴장이 아님"""
    if picker.session_minutes(m, extended=(m == "US")) is None:
        return False
    with in_market(m):
        pf = state().get("preflight") or {}
    return not (pf.get("day") == picker.session_date(m) and pf.get("closed"))


def hourly_text() -> str:
    """시장별 모드·장 상태·보유·손익·매매 횟수·지난 1시간 문제 (아무것도 바꾸지 않음)"""
    lines = [f"⏰ {datetime.now(KST):%H:%M} 장 상황 요약"]
    for m in markets():
        with in_market(m):
            c, s = config(), state()
            flag = "🇺🇸 미장" if m == "US" else "🇰🇷 국장"
            mode = dict(off="꺼짐", paper="가상", live="🔴 실전")[c["mode"]] + (" (자동 가상 전환)" if s.get("auto_paper") and c["mode"] == "paper" else "")
            mins = picker.session_minutes(m, extended=(m == "US"))
            st = "장 닫힘" if mins is None else ("프리장" if mins < 0 else "정규장")
            if mins is not None and not _in_session(m):
                st = "휴장"
            lines.append(f"\n{flag} · {mode} · {st} (현지 {picker.mnow(m):%H:%M})" + (f"\n  ⛔ 오늘 정지: {s['halted_today'][:60]}" if s.get("halted_today") else ""))
            # 보유 종목 (자동매매 것만)
            pos = s["bot_pos"] if c["mode"] == "live" else s["paper"]["positions"]
            pos = {k: v for k, v in pos.items() if _mkt(k) == m and float(v.get("qty") or 0) > 0}
            quotes = {}
            if pos and toss.enabled():
                try:
                    quotes = toss.prices(list(pos))
                except Exception:
                    quotes = {}
            if pos:
                for k, v in pos.items():
                    px, avg = (quotes.get(k) or {}).get("price"), float(v.get("avg") or 0)
                    pct = f" {(px - avg) / avg * 100:+.1f}%" if px and avg else " (시세 없음)"
                    lines.append(f"  • {k} {float(v['qty']):g}주 평단 {avg:g}{pct}")
            else:
                lines.append("  보유 없음")
            # 손익
            lines.append(f"  오늘 실현 {float(s.get('realized_today', 0)):+,.0f}원")
            rl = s.get("realized_lane") or {}
            lines.append("  🤝 " + " · ".join(f"{LANES[st]} {dict(live='실전', paper='가상', off='쉼')[lane_mode(c, st)]} {float(rl.get(st, 0)):+,.0f}원" for st in LANES))
            mo = s.get("month") or {}
            if mo.get("start"):
                pct = float(mo.get("realized", 0)) / mo["start"] * 100
                lines.append(f"  이번 달 {float(mo.get('realized', 0)):+,.0f}원 ({pct:+.1f}% / 목표 {c['monthly_target_pct']:g}%)")
            # 오늘 매수·매도·손절 (자동매매 주문, 거절 제외)
            mk = "live" if c["mode"] == "live" else "paper"
            buy = sell = stop = 0
            for o in s["orders"]:
                if o.get("mode") != mk or o.get("status") == "rejected" or _mkt(o.get("symbol", "")) != m:
                    continue
                ago = _ago_sec(o.get("t", ""))
                if ago is None or picker.session_date(m, datetime.now(KST) - timedelta(seconds=ago)) != s["day"]:
                    continue
                if o.get("side") == "BUY":
                    buy += 1
                else:
                    sell += 1
                    stop += "손절" in str(o.get("reason", ""))
            lines.append(f"  오늘 매수 {buy} · 매도 {sell} (손절 {stop})")
            try:
                lines.append("  " + tl.hot_text(m))
                lines.append("  " + tl.rebound_text(m))
            except Exception:
                pass
            try:   # ⏱️ 1초 감시 초반 신호 결과 (가상)
                from . import watch1s
                lines.append("  " + watch1s.summary(m))
            except Exception:
                pass
            # 지난 1시간 오류·정지
            bad = [e for e in s["events"] if e.get("kind") in ("오류", "정지", "긴급 정지", "주문 거절")
                   and (_ago_sec(e.get("t", "")) if _ago_sec(e.get("t", "")) is not None else 1e9) <= 3600]
            if bad:
                lines.append(f"  ⚠️ 지난 1시간 {len(bad)}건: " + " / ".join(f"[{e['kind']}] {str(e.get('text', ''))[:50]}" for e in bad[-3:]))
    cut = time.time() - 3600
    fails = [msg for t, msg in _save_fails if t >= cut]
    _save_fails[:] = [(t, msg) for t, msg in _save_fails if t >= cut]
    lines.append(f"\n💾 지난 1시간 저장 실패 {len(fails)}건" + (f": {fails[-1][:60]}" if fails else ""))
    return "\n".join(lines)


def _hourly_check() -> None:
    """매시 정각(~5분 안)에 장중 시장이 하나라도 있으면 요약 보내기 (자동매매를 기다리게 하지 않게 따로)"""
    now = datetime.now(KST)
    key = now.strftime("%Y-%m-%d %H")
    if now.minute >= 5 or _hourly_sent[0] == key:
        return
    if not any(_in_session(m) for m in markets()):
        return
    _hourly_sent[0] = key

    def job():
        try:
            from . import notify
            notify.send(hourly_text())
        except Exception as e:
            _log_fn(f"🤖 [오류] 1시간 요약 실패: {type(e).__name__}: {e}")

    threading.Thread(target=job, daemon=True, name="hourly").start()


# ── 백그라운드 ──
def start(log_fn=print) -> None:
    global _thread, _log_fn
    _log_fn = log_fn
    if _thread and _thread.is_alive():
        return
    _resume_note()   # 자동 재시작이었으면 이어받은 상태 기록
    arena._wake = lambda m: _ai_wake[m].set() if m in _ai_wake else None   # 🏟️ AI 리그 판단 끝나면 바로 한 바퀴
    from . import arena_voice   # noqa: F401 — 🗣️ 의견 생긴 AI → 바로 채팅으로 (arena._voice 연결)
    try:
        n_ = extras.purge_test_notes()
        if n_:
            event("정리", f"🧹 시험용 가짜 교훈 {n_}줄을 학습 노트에서 지웠어요")
    except Exception:
        pass

    def tick(m: str, timers: dict) -> None:
        for msg in store.recovered():
            event("복구", msg)
        c = config()
        t = picker.mnow(m)
        # 장 전 준비: 미장은 뉴욕 07:00~ (프리장 켜면 00:30~), 국장은 07:30~ (평일)
        mins = minutes_now(m, c)
        prep_at = ("00:30" if _ext(m, c) else "07:00") if m == "US" else "07:30"
        if c["auto_pick"] and t.weekday() < 5 and t.strftime("%H:%M") >= prep_at:
            ready = (c["strategy"] == "trend" or picker.base_ready(m)) and \
                (c["strategy"] == "surge" or (state().get("swing") or {}).get("day") == picker.session_date(m))
            if not ready and not _busy().is_set():
                prepare_now()
        if mins is not None:
            sd = picker.session_date(m)
            s0 = state()
            if (s0.get("preflight") or {}).get("day") != sd:
                preflight()   # 🩺 그날 첫 장중 확인 전에 실데이터 점검 (휴장일·날짜 기준·형식)
                s0 = state()
            if (s0.get("preflight") or {}).get("closed"):
                return        # 휴장일
        if mins is None:
            # 📚 장 마감 후 하루 한 번 복기 (미장 뉴욕 16:05~, 국장 15:35~)
            s0 = state()
            after = t.strftime("%H:%M") >= ("16:05" if m == "US" else "15:35")
            if after and t.weekday() < 5 and s0.get("reviewed_day") != s0["day"] and s0["day"] == picker.session_date(m):
                with _lock:
                    s1 = state(); s1["reviewed_day"] = s1["day"]; _save(s1)
                review_now()
            return
        # 들고 있는 종목은 자주(기본 10초) 매도 조건만 확인
        gap = c["interval_min"] * 60
        exit_sec = c["exit_check_sec"]
        try:   # 📉 지수 급락일엔 손절 확인을 5초마다 (루프 최소 간격)
            if extras.crash_day(m, c):
                exit_sec = min(exit_sec, 5)
        except Exception:
            pass
        if not (time.time() - timers["last"] >= gap - 5 or time.time() - timers["exit"] < exit_sec):
            with _lock:
                s0 = state()
            if s0["bot_pos"] if c["mode"] == "live" else s0["paper"]["positions"]:
                timers["exit"] = time.time()
                r = run_once(exits_only=True)
                for line in r.get("results", []):
                    if "✅" in line or "📝" in line or "거절" in line or "정지" in line:
                        _log_fn(("🇺🇸 " if m == "US" else "🇰🇷 ") + line)
        if time.time() - timers["last"] < gap and not _ai_wake[m].is_set():
            return
        _ai_wake[m].clear()
        timers["last"] = time.time()
        r = run_once()
        for line in r.get("results", []):
            if "✅" in line or "📝" in line or "거절" in line or "정지" in line:
                _log_fn(("🇺🇸 " if m == "US" else "🇰🇷 ") + line)

    def loop():
        timers = {"US": {"last": 0.0, "exit": 0.0}, "KR": {"last": 0.0, "exit": 0.0}}
        while True:
            time.sleep(5)
            if _stop_req.is_set():
                _idle.set()
                return
            if config()["mode"] == "off":
                continue
            try:   # 📚 쉬는 날 공부 (토요일 오전 · 따로 돌아서 루프는 안 멈춤)
                from . import weekend
                if config().get("weekend_study", True) and weekend.due() and not weekend._running.is_set():
                    threading.Thread(target=weekend.run, args=(_log_fn,), daemon=True, name="weekend").start()
            except Exception as e:
                _log_fn(f"🤖 [오류] 주말 공부: {type(e).__name__}: {e}")
            try:   # 💓 헬스체크 신호 · 💾 하루 한 번 백업
                extras.heartbeat()
                extras.daily_backup(config(), _log_fn)
            except Exception as e:
                _log_fn(f"🤖 [오류] 헬스체크·백업: {type(e).__name__}: {e}")
            try:
                _hourly_check()   # ⏰ 1시간 장 상황 요약 (읽기 전용)
            except Exception as e:
                _log_fn(f"🤖 [오류] 1시간 요약: {type(e).__name__}: {e}")
            for m in markets():   # 국장·미장을 각자 시간에 맞춰
                with in_market(m):
                    try:
                        tick(m, timers[m])
                    except Exception as e:
                        try:
                            event("오류", f"{type(e).__name__}: {e}")
                        except Exception:   # 오류 기록조차 실패해도 자동매매는 계속
                            _log_fn(f"🤖 [오류] {type(e).__name__}: {e}")
                    for msg in store.errors():
                        _save_fails.append((time.time(), msg))   # 1시간 요약용
                        del _save_fails[:-50]
                        _log_fn("💾 " + msg)

    _thread = threading.Thread(target=loop, daemon=True, name="autotrade")
    _thread.start()
    try:   # ⏱️ 2호 급등 후보 1초 감시 (가상 기록만, 주문 없음)
        from . import watch1s
        watch1s.start(log_fn)
    except Exception as e:
        log_fn(f"🤖 [오류] 1초 감시 시작: {type(e).__name__}: {e}")


ASK_WORDS = re.compile(r"자동\s*매매|봇|자동으로\s*(산|판|사|팔)|알아서\s*(산|판)|오늘\s*(매매|거래|수익|손익)|이번\s*달\s*(수익|목표)")


def summary_for_prompt() -> str:
    """채팅 AI 에게 붙여줄 '자동매매가 한 일' 요약 — 돌리는 시장 전부"""
    parts = []
    for m in markets():
        with in_market(m):
            parts.append(f"[{'🇺🇸 미장' if m == 'US' else '🇰🇷 국장'}]\n" + _summary_one().replace("<autotrade>", "").replace("</autotrade>", "").strip())
    return "<autotrade>\n" + "\n\n".join(parts) + "\n</autotrade>"


def _summary_one() -> str:
    """한 시장의 요약 (자동매매가 산 것만 구분해서)."""
    c, s = config(), state()
    mk = "미장" if c["market"] == "US" else "국장"
    lines = ["<autotrade>", "이 앱의 자동매매 기록이에요. '자동매매가 산 주식'은 사장님이 직접 산 주식과 별개예요. 숫자는 여기 값만 쓰고 지어내지 마세요.",
             f"상태: {dict(off='꺼짐', paper='가상 매매', live='실전')[c['mode']]} · {mk} · {STRAT_NAME[c['strategy']]} · "
             f"{'계좌 현금 전부' if c['budget_mode'] == 'all_cash' else '한도 ' + format(c['max_total_krw'], ',') + '원'} · 한 달 목표 {c['monthly_target_pct']}%"
             + (f" · 오늘 정지됨({s['halted_today']})" if s.get("halted_today") else "")]
    pos = s["bot_pos"] if c["mode"] != "paper" else s["paper"]["positions"]
    lines.append("자동매매가 지금 가진 주식: " + (", ".join(f"{k} {v['qty']:g}주(평단 {v['avg']:g}, {'스윙' if s['style'].get(k) == 'swing' else '단타'})" for k, v in pos.items()) or "없음"))
    lines.append(f"오늘({s['day']}) 실현 손익: {float(s.get('realized_today', 0)):+,.0f}원 · 오늘 매수 {s['orders_today']}번")
    mo = s.get("month") or {}
    if mo.get("start"):
        lines.append(f"이번 달 실현 손익: {float(mo.get('realized', 0)):+,.0f}원 ({float(mo.get('realized', 0)) / mo['start'] * 100:+.1f}%, 시작 예산 {mo['start']:,.0f}원)")
    recent = s["orders"][-12:]
    if recent:
        lines.append("최근 주문:")
        lines += [f"- {o['t']} {o['symbol']} {'매수' if o['side'] == 'BUY' else '매도'} {o['qty']}주 @ {o['price']:g} · {o.get('status', '')} · {o.get('reason', '')}" for o in recent]
    pk = s.get("picks") or {}
    if pk.get("picks"):
        lines.append(f"최근 고른 종목({pk.get('time', '')}): " + ", ".join(f"{x['name']}({x['symbol']})" for x in pk["picks"]))
    rv = tl.last_review_text("live" if c["mode"] == "live" else "paper", c["market"])
    if rv:
        lines.append(rv)
    try:   # 🧯 손절 기록 (기준 대비 차이·원인·손절 뒤 변화)
        sl = stop_log_text()
        if sl:
            lines.append(sl)
        ss = stop_slip_view().get("text")   # 🔻 손절 슬리피지 따로
        if ss:
            lines.append(ss)
    except Exception:
        pass
    vd = s.get("verify_daily") or {}
    if vd.get("text"):   # 🧾 하루 한 번 검증 누적 요약 (실전 전환 판단 근거)
        lines.append(f"[{vd.get('t', '')} 기준] " + vd["text"].replace("\n", " / "))
    else:   # 아직 한 번도 안 돌았으면 숫자 지어내지 않게
        lines.append("🧾 검증 누적 요약: 아직 없음 (평일 장 마감 복기 뒤 하루 한 번 만들어져요)")
    lines.append("</autotrade>")
    return "\n".join(lines)



# ── 채팅으로 엔진과 대화 (🤖 자동매매 팀) ──
LABELS = {"live_quality": "실전은 강한 신호만", "live_quality_change": "강한 신호 최소 상승률(%)", "live_quality_ratio": "강한 신호 거래량 배수",
          "swing_live": "스윙 실전", "live_needs_proof": "검증된 조건만 실전", "live_proof_n": "실전 검증 최소 건수", "time_stop_min": "시간 손절(분)",
          "time_stop_up_pct": "시간 손절 최소 상승(%)", "stop_slip_pct": "손절 매도 지정가 여유(%)", "stop_resell_sec": "매도 재주문(초)",
          "surge_min_change_regular": "정규장 급등 최소 상승률(%)", "surge_buy_cutoff_min": "급등주 매수 마감(분)", "open_weak_skip": "초반 약세장 건너뛰기", "open_weak_min": "초반 기준(분)", "open_weak_idx_pct": "초반 지수 -%면 안 삼","regime_on": "시장 분위기 필터", "regime_half_pct": "지수 -%면 급등주 절반", "regime_stop_pct": "지수 -%면 급등주 안 삼",
          "tod_filter": "지는 시간대 피하기", "max_per_sector": "같은 업종 최대 보유", "backup_on": "매일 백업", "weekly_report": "주간 성적표",
          "tomorrow_plan": "내일 계획 알림", "weekend_study": "주말 공부", "improve_effect_check": "자동 개선 효과 확인", "lane_swing_mode": "1호 일반(스윙) AI 모드", "lane_surge_mode": "2호 급등주 AI 모드", "lane_share_surge": "2호 예산 몫(%)", "lane_auto_share": "예산 몫 자동 조정", "lane_auto_live": "2호 가상→실전 자동", "lane_cross_review": "두 AI 교차 복기", "arena_on": "AI 리그", "swing_auto_off": "스윙 성적 나쁘면 자동으로 쉬기", "arena_replay": "AI 리그 과거 장 복습", "arena_replay_per_day": "하루 과거 장 복습 후보 수", "arena_replay_market": "과거 장 복습에 시장 전체 급등일 포함", "arena_size": "AI 리그 참가자 수", "arena_ai_per_day": "AI 리그 하루 AI 판단 횟수 (0=제한 없음)", "arena_min_trades": "AI 리그 실전 자격 최소 거래", "arena_promote": "AI 리그 1등 전략 참고", "arena_live": "AI 리그 상위 AI 실전 매수", "arena_live_n": "AI 리그 실전 자격 인원", "arena_live_pct": "AI 리그 실전 예산 몫(%)", "arena_ctrl_margin": "AI 리그 대조군보다 나아야 할 폭(%p)", "arena_half_life_days": "AI 리그 성적 반감 일수", "arena_voice": "리그 AI 의견 바로 채팅으로", "arena_voice_every": "의견 물어볼 거래 간격(건)", "arena_voice_per_day": "하루 최대 의견 수", "notes_council": "노트 정리 회의", "daily_digest": "제안 현황 하루 요약","no_rebuy_after_loss": "손해 본 종목 재진입 금지", "no_rebuy_swing_days": "스윙 재진입 금지 일수", "explore_per_day": "하루 탐색 매수 횟수", "ai_take_profit": "AI 익절 (off/test/on)", "ai_tp_guard_peak": "AI 익절 안전장치 최고 수익(%)", "ai_tp_guard_floor": "AI 익절 안전장치 바닥(%)", "news_unknown_ratio": "이유 모를 때 금액 배수", "focus_max_pct": "확신 집중 최대 비중(%)", "focus_min_samples": "확신 집중 최소 표본(건)", "focus_min_ev_pct": "확신 집중 최소 기대 수익(%)", "monthly_target_pct": "한 달 목표(%)", "pick_count": "동시 보유 종목 수", "max_total_krw": "총 투자 한도(원)",
          "max_order_krw": "한 번 주문 최대(원)", "max_position_krw": "종목당 최대(원)", "max_orders_per_day": "하루 매수 횟수",
          "max_daily_loss_pct": "하루 손실 한도(예산 %)", "max_daily_loss_krw": "하루 손실 한도(원)", "stop_loss_pct": "손절(%)",
          "take_profit_pct": "남은 물량 익절(%)", "partial_tp_pct": "나눠 팔기 수익률(%)", "partial_tp_ratio": "나눠 팔기 비율",
          "trail_pct": "고점 대비 하락 매도(%)", "trail_wide_after_pct": "넓은 기준 쓰는 최고 수익(%)", "trail_wide_pct": "넓은 고점 대비 기준(%)", "surge_min_change_pct": "급등 최소 상승률(%)", "surge_max_change_pct": "급등 최대 상승률(%)",
          "surge_volume_ratio": "거래량 배수 기준", "pre_volume_ratio": "프리장 거래량 기준", "min_trading_value_krw": "최소 거래대금(원)",
          "us_min_price": "최소 주가($)", "limit_slip_pct": "지정가 여유(%)", "chase_filter": "고점 추격 필터(2호)", "falling5_block": "5분 하락 중 매수 금지(2호)",
          "chase_near_high_pct": "고점 근처 기준(%)", "chase_min_pullback_pct": "직전 5분 최소 되돌림(%)", "chase_run5_max_pct": "고점 근처 5분 급등 한도(%)",
          "rebound_entry": "반등 확인 후 진입(2호)", "rebound_pullback_pct": "반등 전 최소 눌림(%)", "rebound_pullback_max_pct": "반등 전 최대 눌림(%)", "rebound_up_bars": "연속 상승 1분봉 수", "interval_min": "종목 찾기 간격(분)",
          "exit_check_sec": "손절 확인 간격(초)", "loss_streak_pause": "연속 손실 쉬기 기준", "pause_min": "쉬는 시간(분)",
          "us_premarket": "프리장 매매", "stop_at_target": "목표 달성 시 쉬기", "ai_check": "주문 전 AI 검토", "auto_pick": "종목 알아서 고르기",
          "market": "시장", "strategy": "전략", "budget_mode": "예산 방식", "mode": "모드",
          "stop_min": "손절 자동 최소(%)", "stop_max": "손절 자동 최대(%)", "limit_min_mult": "한도 자동 최소 배수", "limit_max_mult": "한도 자동 최대 배수",
          "swing_stop_pct": "스윙 손절(%)", "swing_trail_pct": "스윙 고점 대비(%)", "swing_trail_vol": "스윙 고점 대비 변동성 자동", "swing_trail_vol_mult": "스윙 변동성 배수","swing_max_days": "스윙 최대 보유일",
          "ai_decide": "거래마다 AI 판단", "shadow_on": "그림자 가상 매매", "auto_promote": "규칙 자동 교체", "backtest_on": "백테스트",
          "loss_pct_min": "손실 한도 자동 최소(%)", "loss_pct_max": "손실 한도 자동 최대(%)", "auto_tune": "자동 조절", "auto_mode": "실전↔가상 자동"}
TRADE_GUIDE = """[자동매매 행동 태그 — 사장님이 원할 때만, 답 맨 아래에]
- 설정 바꾸기: [[TRADE_SET: 키=값 | 이유]] (여러 개면 줄마다 하나). 키는 <engine> 의 '설정' 목록에 있는 영문 키만. 예: [[TRADE_SET: partial_tp_pct=5 | 나눠 팔기를 조금 일찍]]
- 모드: [[TRADE_MODE: paper | 이유]] 또는 [[TRADE_MODE: off | 이유]] (live 는 채팅으로 불가)
- 긴급 정지: [[TRADE_STOP: 이유]]
- 종목 제외/해제: [[TRADE_EXCLUDE: TSLA | 이유]] · [[TRADE_INCLUDE: TSLA]]
태그는 바로 실행되지 않고 채팅에 [적용] 카드로 떠요. 사장님이 원하지 않았는데 태그를 쓰지 마세요."""


def engine_context() -> str:
    """🤖 자동매매 팀이 볼 엔진 전체 상태 — 돌리는 시장 전부"""
    parts = []
    for m in markets():
        with in_market(m):
            parts.append(f"[{'🇺🇸 미장' if m == 'US' else '🇰🇷 국장'}]\n" + _engine_one().replace("<engine>", "").replace("</engine>", "").strip())
    return "<engine>\n" + "\n\n".join(parts) + "\n</engine>"


def _engine_one() -> str:
    c, s = config(), state()
    mode_key = "live" if c["mode"] == "live" else "paper"
    m = minutes_now(c["market"], c)
    lines = ["<engine>", _summary_one().replace("<autotrade>", "").replace("</autotrade>", "").strip(),
             "설정 (키 = 값 · 뜻): " + " / ".join(f"{k}={c[k]}({LABELS[k]})" for k in LABELS if k in c),
             f"제외 종목: {', '.join(c['exclude']) or '없음'} · 직접 고른 종목: {', '.join(c['symbols']) or '없음'}",
             f"시장 시각: 현지 {picker.mnow(c['market']).strftime('%H:%M')} · "
             + ("장 닫힘" if m is None else ("프리장" if m < 0 else f"정규장 {m}분째"))
             + f" · 다음 종목 찾기 {_next_open(c) or '-'} (한국 시간)"]
    if s.get("cands"):
        lines.append("최근 후보(기대 수익률 순): " + " / ".join(f"{k}({'스윙' if v['style'] == 'swing' else '단타'}, 기대 {v['ev']:+.2f}%)" for k, v in
                                                       sorted(s["cands"].items(), key=lambda kv: -kv[1]["ev"])[:8]))
    pk = s.get("picks") or {}
    if pk.get("picks") or pk.get("note"):
        lines.append(f"최근 종목 찾기 결과({pk.get('time', '')}): {pk.get('note', '')}")
        lines += [f"- {x['name']}({x['symbol']}) 점수 {x['score']} · {x['reason']}" for x in pk.get("picks", [])]
    lv = lanes_overview(c["market"])
    lines.append("🤝 두 AI: " + " / ".join(f"{v['name']} 모드 {v['mode']} 몫 {v['share']}% · 📏 {(v.get('ss') or {}).get('text', '')}"
                                        for v in lv.values()))
    try:
        lines.append("📊 " + _scorecard_safe(c, s)["text"].replace("\n", " / "))
        gt = extras.gate_text(c["market"], s["day"])
        if gt:
            lines.append(gt)
        try:
            from . import arena_replay as _ar
            rt_ = _ar.text(c["market"])
            if rt_:
                lines.append(rt_)
        except Exception:
            pass
        et_ = _edge_text(c["market"])
        if et_:
            lines.append("🎯 판단 질 점검: " + et_)
        at_ = arena.text(c["market"])
        if at_:
            lines.append(at_)
        rh = extras.rule_history(5)
        if rh:
            lines.append("🧮 최근 자동 규칙 변경: " + " / ".join(f"{x['t'][5:16]} {', '.join(f'{k} {x['before'].get(k)}→{v}' for k, v in x['after'].items())}"
                                                       + (f" ({x['result'][:30]})" if x.get("result") else "") for x in rh))
    except Exception:
        pass
    notes = tl.lessons_text(c["market"], 8)
    if notes:
        lines.append("📒 공유 노트(교차 복기 교훈): " + notes)
    ev = s["events"][-25:]
    if ev:
        lines.append("최근 활동 기록:")
        lines += [f"- {e['t']} [{e['kind']}] {e['text']}" for e in ev]
    L = tl.overview(s["day"], mode_key, c["market"])
    td, g = L["today"], L["today"].get("guard", {})
    line = f"오늘 학습: 끝난 거래 {td.get('n', 0)}건"
    if td.get("n"):
        line += f" · {'승률 ' + str(td['win_rate']) + '%' if td['n'] >= 20 else '표본 부족(' + str(td['n']) + '건)'} · 평균 {td['avg']:+.2f}%"
    if (L.get("since") or {}).get("text"):   # 📏 기준 이후 누적
        line += f" · 누적 {L['since']['text']}"
    if td.get("open"):
        line += " · 보유 중 " + ", ".join(f"{o['sym']} 최대{o['mfe']:+.1f}%/최저{o['mae']:+.1f}%" for o in td["open"])
    if g.get("pause_until") or g.get("no_premarket"):
        line += f" · 조심 규칙: {g.get('pause_why') or ''} {g.get('no_premarket') or ''}"
    lines.append(line)
    try:
        lines.append(tl.hot_text(c["market"]))
        lines.append(tl.rebound_text(c["market"]))
    except Exception:
        pass
    for t in L["trades"][:10]:
        f = t.get("features") or {}
        lines.append(f"- 일지 {t['entry_time'][5:16]} {t['sym']} " + (f"{t.get('pnl_pct', 0):+.2f}% {t.get('exit_kind', '')}" if t["closed"] else "보유 중")
                     + f" · 최대 {t['mfe']:+.1f}% 최저 {t['mae']:+.1f}% · " + _entry_text(f))
    if L["proposals"]:
        lines.append("학습 제안: " + " / ".join(f"{p['label']} {p['from']}→{p['to']}({'적용됨' if p['status'] == 'applied' else '대기'}: {p['why']})"
                                              for p in L["proposals"][:6]))
    lines.append("</engine>")
    return "\n".join(lines)


def _entry_text(f: dict) -> str:
    """일지의 '살 때 조건' 문구 (스윙/단타 따로, 값 없으면 '-')"""
    def v(k, fmt="{:g}"):
        return fmt.format(f[k]) if isinstance(f.get(k), (int, float)) else "-"
    vt = f" · 거래량 추세 {f['vtrend']}({v('vtrend_r')}배)" if f.get("vtrend") else ""   # 📈 진입 시점 거래량 추세
    if f.get("style") == "swing":
        val = f"{f['value20'] / 1e8:,.0f}억원" if isinstance(f.get("value20"), (int, float)) else "-"
        return (f"살 때 [스윙] 20일 {v('r20', '{:+.1f}')}% · 60일 {v('r60', '{:+.1f}')}% · RSI {v('rsi', '{:.0f}')} · 거래대금 {val}/일"
                f" · 변동성 {v('volat', '{:.0f}')}% · 당일 {v('chg1d', '{:+.1f}')}% · 거래량 {v('vratio')}배{vt}")
    return f"살 때 {v('change')}% 거래량 {v('ratio')}배{' 프리장' if f.get('pre') else ''}{vt}"


_ACT = re.compile(r"\[\[TRADE_(SET|MODE|STOP|EXCLUDE|INCLUDE):\s*([^\]|]*?)\s*(?:\|\s*([^\]]*?))?\s*\]\]")


def find_actions(reply: str) -> list[dict]:
    """AI 답의 행동 태그 → [적용] 카드용 (실행은 안 함)"""
    c = config()
    out = []
    for kind, arg, why in _ACT.findall(reply or ""):
        a = {"id": uuid.uuid4().hex[:8], "type": kind.lower(), "why": (why or "").strip()}
        if kind == "SET":
            if "=" not in arg:
                continue
            k, v = [x.strip() for x in arg.split("=", 1)]
            if k not in LABELS or k == "mode" or (k.startswith("lane_") and k.endswith("_mode") and v.strip().lower() == "live") or (k == "arena_live" and v.strip().lower() in ("true", "1", "on", "켜기", "yes")):   # 실전 켜기는 채팅 불가 (AI 별 모드도)
                continue
            a.update(param=k, value=v, label=f"{LABELS[k]}: {c.get(k)} → {v}")
        elif kind == "MODE":
            v = arg.strip().lower()
            if v not in ("paper", "off"):
                continue
            a.update(value=v, label=f"모드: {c['mode']} → {dict(paper='가상 매매', off='끄기')[v]}")
        elif kind == "STOP":
            a.update(label="⛔ 긴급 정지 (꺼짐 + 자동매매 미체결 주문 취소)", why=(arg or why or "").strip())
        else:
            sym = toss.symbol(arg.strip())
            if not re.fullmatch(r"\d{6}|[A-Z][A-Z.\-]{0,9}", sym):
                continue
            a.update(value=sym, label=f"{'사지 않을 종목에 추가' if kind == 'EXCLUDE' else '제외 해제'}: {sym}")
        out.append(a)
    return out[:8]


def apply_action(a: dict) -> dict:
    """사장님이 [적용]을 누른 행동 실행 (설정 검사는 save_config 가 함)"""
    t = a.get("type")
    c = config()
    if t == "set":
        k, v = a.get("param"), a.get("value")
        if k not in LABELS or k == "mode" or (k.startswith("lane_") and k.endswith("_mode") and str(v).strip().lower() == "live") or (k == "arena_live" and str(v).strip().lower() in ("true", "1", "on", "켜기", "yes")):
            return {"ok": False, "message": "바꿀 수 없는 설정이에요 (실전 켜기는 🤖 화면에서만)"}
        if isinstance(DEFAULTS.get(k), bool):
            v = str(v).strip().lower() in ("1", "true", "on", "켜기", "yes", "예")
        r = save_config({k: v})
    elif t == "mode":
        if a.get("value") not in ("paper", "off"):
            return {"ok": False, "message": "채팅으로는 가상 매매·끄기만 돼요 (실전은 🤖 화면에서)"}
        r = save_config({"mode": a["value"]})
    elif t == "stop":
        r = emergency_stop()
    elif t == "notes_undo":   # 📒 노트 정리 되돌리기
        if a.get("market") not in ("US", "KR"):
            return {"ok": False, "message": "시장이 이상해요"}
        r = tl.undo_core(a["market"])
    elif t == "arena":   # 🗣️ 리그 AI 자기 규칙 (가상 전용)
        r = arena.set_rules(str(a.get("market") or ""), str(a.get("ai") or ""), a.get("changes") or {}, str(a.get("habit") or ""))
    elif t == "arena_undo":   # 🏟️ AI 가 스스로 바꾼 규칙 되돌리기
        if a.get("market") not in ("US", "KR"):
            return {"ok": False, "message": "시장이 이상해요"}
        r = arena.undo_rules(a["market"], str(a.get("ai") or ""), str(a.get("t") or ""))
    elif t in ("exclude", "include"):
        ex = [x for x in c["exclude"] if x != a.get("value")] + ([a["value"]] if t == "exclude" else [])
        r = save_config({"exclude": ex})
    else:
        return {"ok": False, "message": "모르는 행동이에요"}
    if r.get("ok"):
        event("채팅 지시", f"{a.get('label', t)}" + (f" — {a['why']}" if a.get("why") else ""))
    return r


# ── 📊 차트 ──
def _ai_chart_ok(sym: str, reason: str, price: float) -> tuple[bool, str, str]:
    """사기 직전: 오늘 1분봉 차트를 그려서 AI 가 그림을 보고 판단. 실패하면 안전하게 '반대'."""
    from . import chart
    from . import providers as pv
    from . import team as tm
    try:
        name = chart.draw(sym, "1m", title=sym)
    except Exception as e:
        return False, f"차트를 못 그렸어요({type(e).__name__})", ""
    prompt = (f"{sym} 급등주 매수 직전이에요. 규칙상 이유: {reason}. 현재가 {price}.\n첨부한 오늘 1분봉 차트(캔들·MA5/20·VWAP·거래량)를 보고 "
              "지금 사도 되는지 판단하세요. 꼭대기에서 꺾이는 중·거래량이 줄며 밀리는 중·VWAP 아래로 이탈이면 반대, "
              "눌림 후 재상승·거래량 늘며 고점 돌파·VWAP 위 지지면 찬성. 한 줄 이유 후 마지막 줄에 '판정: 찬성' 또는 '판정: 반대'.")
    try:
        reply = pv.chat(tm.fast_spec("claudecode:sonnet"), "너는 단타 차트 판독가다. 짧게.", [{"role": "user", "content": prompt}],
                        effort="low", timeout=90, images=[str(chart.path(name))])
    except Exception as e:
        return False, f"AI 차트 확인 실패({type(e).__name__})", name
    last = [l for l in reply.strip().splitlines() if "판정" in l][-1:] or [""]
    ok = "찬성" in last[0] and "반대" not in last[0]
    return ok, reply.strip().splitlines()[0][:160], name


_live_cache: dict[str, dict] = {}


def auto_charts(limit: int = 6, open_sym: str = "") -> list[dict]:
    """📊 실시간 차트: 자동매매가 가진 종목 + 방금 고른 종목 (최대 6개). 종목마다 한 줄 요약, 펼친 종목(open_sym)만 차트 그림 (1분에 한 번)"""
    c, s = config(), state()
    if not toss.enabled():
        return []
    pos = s["bot_pos"] if c["mode"] != "paper" else s["paper"]["positions"]
    held = list(pos.keys())
    pk = (s.get("picks") or {}).get("picks", [])
    picks = list((s.get("cands") or {}).keys()) or [x["symbol"] for x in pk]
    names = {x.get("symbol"): x.get("name") for x in pk if x.get("symbol")}
    syms = list(dict.fromkeys(held + picks))[:limit]
    try:   # 한 줄 요약용 현재가 (한 번에 조회)
        qs = toss.prices(syms) if syms else {}
    except Exception:
        qs = {}
    out = []
    for sym in syms:
        px = (qs.get(sym) or {}).get("price")
        avg = (pos.get(sym) or {}).get("avg") or 0
        row = {"sym": sym, "held": sym in held, "name": names.get(sym) or "", "price": px,
               "pnl_pct": round((px / avg - 1) * 100, 2) if px and avg else None}
        if sym != open_sym:   # 접힌 종목은 차트 안 그림
            out.append(row)
            continue
        hit = _live_cache.get(sym)
        if not hit or time.time() - hit["at"] > 55:
            try:
                r = draw_chart(sym, "1m", live=True)
                hit = _live_cache[sym] = {**r, "at": time.time()}
            except Exception as e:
                hit = {"error": str(e), "at": time.time()}
                _live_cache[sym] = hit
        out.append({**row, **{k: v for k, v in hit.items() if k != "at"}, "v": int(hit["at"])})
    return out


def draw_chart(symbol: str, interval: str = "1m", live: bool = False) -> dict:
    with in_market(_mkt(toss.symbol(symbol or "X"))):   # 그 종목 시장의 기록 기준
        return _draw_chart(symbol, interval, live)


def _draw_chart(symbol: str, interval: str = "1m", live: bool = False) -> dict:
    """화면용 차트: 자동매매가 가진 종목이면 산 가격·손절·나눠 팔기·익절·고점 대비 하락선과 매수/매도 표시"""
    from . import chart
    sym = toss.symbol(symbol or "")
    if not re.fullmatch(r"\d{6}|[A-Z][A-Z.\-]{0,9}", sym):
        raise ValueError("종목 코드를 넣어 주세요 (예: NVDA, 005930)")
    if not toss.enabled():
        raise ValueError("토스 키가 있어야 차트를 그려요 (🧰 도구)")
    c, s = config(), state()
    pos = (s["bot_pos"] if c["mode"] != "paper" else s["paper"]["positions"]).get(sym)
    lines = {}
    if pos and pos.get("avg"):
        a = pos["avg"]
        lines = {"산 가격": a, f"손절 -{c['stop_loss_pct']:g}%": a * (1 - c["stop_loss_pct"] / 100),
                 f"익절 +{c['take_profit_pct']:g}%": a * (1 + c["take_profit_pct"] / 100)}
        if c.get("partial_tp_pct") and not s["partial"].get(sym):
            lines[f"나눠 팔기 +{c['partial_tp_pct']:g}%"] = a * (1 + c["partial_tp_pct"] / 100)
        hi = s["highs"].get(sym)
        if hi:
            tr = trail_for(a, hi, c)
            lines[f"고점 -{tr:g}%"] = hi * (1 - tr / 100)
    marks = []
    for o in s["orders"][-80:]:
        if o["symbol"] == sym and o.get("status") not in ("rejected",):
            try:
                t = datetime.strptime(f"{datetime.now(KST).year}-{o['t']}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST).isoformat()
            except ValueError:
                continue
            marks.append({"t": t, "side": o["side"], "px": o["price"]})
    name = chart.draw(sym, "1d" if interval == "1d" else "1m", lines=lines, marks=marks if interval == "1m" else None, title=sym,
                      name=f"live-{sym}-{interval}.png" if live else "")
    return {"id": name, "label": f"{sym} {'일봉' if interval == '1d' else '1분봉'}", "lines": {k: round(v, 4) for k, v in lines.items()}}



# ── 🔁 자동 조절 (매일 복기 직후, 범위 안에서만) ──
def _step(old, to, max_rel: float = 0.3):
    """자동 적용은 하루에 최대 ±30% 만큼만 (한 번에 크게 바뀌지 않게, 숫자가 아니면 그대로)"""
    if isinstance(old, bool) or isinstance(to, bool) or not isinstance(old, (int, float)) or not old:
        return to
    lim = abs(old) * max_rel
    v = old + max(-lim, min(lim, float(to) - old))
    return round(v, 2)


def auto_adjust(rep: dict, mode_key: str) -> list[str]:
    c = config()
    done: list[str] = []
    if c["mode"] == "off":   # 꺼져 있으면 아무것도 안 바꿈
        return done
    sums = tl.day_sums(mode_key, 5, c["market"])
    last_good = bool(sums) and sums[-1] > 0
    two_bad = len(sums) >= 2 and sums[-2] < 0 and sums[-1] < 0

    def change(param: str, new, why: str, evidence: str):
        old = c.get(param)
        if old == new:
            return
        r = save_config({param: new}, _by_auto=True)
        if r.get("ok"):
            tl.record_auto(param, LABELS.get(param, param), old, new, why, evidence)
            event("자동 조정", f"{LABELS.get(param, param)} {old} → {new} — {why} ({evidence})")
            c[param] = new
            done.append(param)

    # 1) 학습 제안 자동 적용 (손절은 stop_min~stop_max 로 자름)
    if c["auto_rules"] or c["auto_stop"]:
        pend = {x["id"]: x for x in tl.overview(state()["day"], mode_key, c["market"])["proposals"] if x["status"] == "pending"}
        for pid in rep.get("proposals") or []:
            p = pend.get(pid)
            if not p:
                continue
            if p["param"] == "stop_loss_pct":
                if c["auto_stop"]:
                    to = min(c["stop_max"], max(c["stop_min"], _step(c["stop_loss_pct"], float(p["to"]))))
                    if tl.decide(pid, "apply", auto=True, to=to).get("ok"):
                        done.append("stop_loss_pct")
            elif c["auto_rules"] and tl.decide(pid, "apply", auto=True, to=_step(c.get(p["param"]), p["to"])).get("ok"):
                done.append(p["param"])
        c = config()

    r30 = rep.get("recent") or {}
    # 2) 종목당·주문 한도: 최근 성적 좋으면 +20%, 나쁘면 -20% (처음 값의 배수 범위 안)
    if c["auto_limits"] and c["max_position_krw"] > 0 and r30.get("n", 0) >= 10:
        with _lock:
            s = state()
            base = s.setdefault("limit_base", {"position": c["max_position_krw"], "order": c["max_order_krw"]})
            _save(s)
        lo, hi = base["position"] * c["limit_min_mult"], base["position"] * c["limit_max_mult"]
        cur = c["max_position_krw"]
        new, why = cur, ""
        if r30["avg"] < 0 or two_bad:   # 줄이는 건 먼저 (최근 30건이 마이너스거나 2일 연속 손실)
            new, why = round(max(lo, cur * 0.8) / 1000) * 1000, "최근 성적이 나빠서 줄임" if r30["avg"] < 0 else "2일 연속 손실이라 줄임"
        elif r30["avg"] > 0 and r30["win_rate"] >= 45 and last_good:   # 늘리는 건 최근 30건 좋고 + 어제(마지막 날)도 수익일 때만
            new, why = round(min(hi, cur * 1.2) / 1000) * 1000, "최근 성적이 좋고 마지막 날도 수익이라 늘림"
        if new != cur:
            ev = f"최근 {r30['n']}건 승률 {r30['win_rate']}% · 평균 {r30['avg']:+.2f}% · 범위 {lo:,.0f}~{hi:,.0f}원"
            change("max_position_krw", new, why, ev)
            if c["max_order_krw"] > 0:
                o_lo = (base["order"] or base["position"]) * c["limit_min_mult"]
                change("max_order_krw", round(min(new, max(o_lo, c["max_order_krw"] * new / cur)) / 1000) * 1000, why, ev)

    # 3) 하루 손실 한도: 2일 연속 손실이면 조이고, 3일 연속 수익이면 늘림
    if c["auto_loss"] and len(sums) >= 2:
        three_good = len(sums) >= 3 and all(x > 0 for x in sums[-3:])
        ev_bad = f"최근 하루 손익 {sums[-2]:+.1f}%, {sums[-1]:+.1f}%"
        ev_good = "최근 하루 손익 " + ", ".join(f"{x:+.1f}%" for x in sums[-3:])
        if c["budget_mode"] == "all_cash":
            cur = c["max_daily_loss_pct"]
            if two_bad:
                change("max_daily_loss_pct", max(c["loss_pct_min"], round(cur - 1, 1)), "2일 연속 손실이라 하루 손실 한도를 조임", ev_bad)
            elif three_good:
                change("max_daily_loss_pct", min(c["loss_pct_max"], round(cur + 1, 1)), "3일 연속 수익이라 하루 손실 한도를 늘림", ev_good)
        else:
            with _lock:
                s = state(); lb = s.setdefault("loss_base", c["max_daily_loss_krw"]); _save(s)
            cur = c["max_daily_loss_krw"]
            if two_bad:
                change("max_daily_loss_krw", int(max(lb * 0.5, cur * 0.8)), "2일 연속 손실이라 하루 손실 한도를 조임", ev_bad)
            elif three_good:
                change("max_daily_loss_krw", int(min(lb * 2, cur * 1.2)), "3일 연속 수익이라 하루 손실 한도를 늘림", ev_good)

    # 4) 실전 ↔ 가상 (끄기 상태는 건드리지 않음)
    if c["auto_mode"]:
        with _lock:
            s = state()
        mo = s.get("month") or {}
        month_pct = float(mo.get("realized", 0)) / mo["start"] * 100 if mo.get("start") else 0
        if c["mode"] == "live":
            k = int(c["mode_down_days"])
            bad_days = len(sums) >= k and all(x < 0 for x in sums[-k:])
            if bad_days or month_pct <= -c["mode_down_month_pct"]:
                why = f"{k}일 연속 손실" if bad_days else f"이번 달 {month_pct:+.1f}% (기준 -{c['mode_down_month_pct']:g}%)"
                if save_config({"mode": "paper"}, _by_auto=True).get("ok"):
                    with _lock:
                        s = state(); s["auto_paper"] = True; s["auto_paper_since"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"); _save(s)
                    tl.record_auto("mode", "모드", "live", "paper", "성적이 나빠 가상으로 내림", why)
                    event("자동 조정", f"모드 실전 → 가상 — {why}. 가상 성적이 회복되면 실전으로 자동 복귀")
                    done.append("mode")
        elif c["mode"] == "paper" and s.get("auto_paper") and s.get("user_live_ok") and toss.enabled():
            st_ = tl._stats(tl.trades_since("paper", s.get("auto_paper_since", ""), c["market"]))
            if st_.get("n", 0) >= c["mode_up_trades"] and st_["win_rate"] >= 50 and st_["avg"] > 0:
                if save_config({"mode": "live"}, _by_auto=True).get("ok"):
                    with _lock:
                        s = state(); s["auto_paper"] = False; _save(s)
                    ev = f"가상 {st_['n']}건 승률 {st_['win_rate']}% · 평균 {st_['avg']:+.2f}%"
                    tl.record_auto("mode", "모드", "paper", "live", "가상 성적이 회복돼 실전으로 복귀", ev)
                    event("자동 조정", f"모드 가상 → 실전 — {ev}")
                    done.append("mode")
    if done:
        event("자동 조정", f"오늘 자동으로 바꾼 것: {', '.join(LABELS.get(x, x) for x in dict.fromkeys(done))}")
    return done


def halt_release_kst() -> str:
    """오늘 정지가 자동으로 풀리는 한국 시각 (그 시장의 날짜가 바뀌는 때)"""
    c = config()
    now = datetime.now(KST)
    day = picker.session_date(c["market"])
    for m in range(0, 60 * 48, 5):
        t = now + timedelta(minutes=m)
        if picker.session_date(c["market"], t) != day:
            return ("오늘 " if t.date() == now.date() else "내일 ") + t.strftime("%H:%M")
    return ""


def unhalt() -> dict:
    """사장님이 직접 오늘 정지 풀기 (화면 버튼 — 채팅으로는 불가)"""
    with _lock:
        s = state()
        was = s.get("halted_today", "")
        if not was:
            return {"ok": False, "message": "지금 정지 상태가 아니에요"}
        s["halted_today"] = ""
        s["day_budget"] = 0.0
        _save(s)
    event("정지 해제", f"사장님이 직접 오늘 정지를 풀었어요 (이전 이유: {was})")
    return {"ok": True, "message": "정지를 풀었어요. 하루 손실 한도는 지금 예산 기준으로 다시 계산돼요"}


# ── 🤖 거래마다 AI 판단 ──
def _arena_ov(market: str) -> dict:
    try:
        return arena.overview(market, config())
    except Exception:
        return {}


def _replay_text(market: str) -> str:
    try:
        from . import arena_replay
        return arena_replay.text(market)
    except Exception:
        return ""


def _edge_text(market: str) -> str:
    try:
        return edge.text(market)
    except Exception:
        return ""


def _edge_step(market: str, quotes: dict, minutes, day: str, dry: bool) -> None:
    """🤖 AI 가 막은 종목 채점 (시세만 읽음 · 실패해도 매매 영향 없음)"""
    if dry:
        return
    try:
        need = [x for x in edge.veto_syms(market) if x not in quotes]
        q = dict(quotes)
        if need:
            q.update(toss.prices(need[:60]))
        edge.veto_tick(market, q, minutes, day)
    except Exception:
        pass


def _arena_step(c: dict, market: str, cands: dict, quotes: dict, minutes, day: str, dry: bool, exits_only: bool, hot: dict | None = None) -> list[str]:
    """🏟️ AI 리그 한 바퀴 (가상 전용 · 실패해도 매매에 영향 없음)"""
    if not c.get("arena_on") or dry:
        return []
    try:
        return arena.step(c, market, {} if exits_only else cands, quotes, minutes, day,
                          price_fn=lambda xs: toss.prices(xs), ai_fn=arena.ai_json, extra={} if exits_only else (hot or {}))
    except Exception as e:
        return [f"🏟️ AI 리그 오류: {type(e).__name__}: {e}"]


def _ai_budget_ok(c: dict) -> bool:
    """시간당 AI 판단 횟수 제한 (넘으면 규칙대로)"""
    now = time.time()
    with _lock:
        s = state()
        calls = [t for t in s.get("ai_calls", []) if now - t < 3600]
        if len(calls) >= c["ai_max_per_hour"]:
            return False
        calls.append(now)
        s["ai_calls"] = calls; _save(s)
    return True


def _ai_json(prompt: str, image: str | None) -> dict | None:
    from . import providers as pv
    from . import team as tm
    try:
        reply = pv.chat(tm.fast_spec("claudecode:sonnet"), "너는 신중한 트레이더다. JSON 한 줄로만 답한다.",
                        [{"role": "user", "content": prompt}], effort="low", timeout=90, images=[image] if image else [])
    except Exception:
        return None
    m = re.search(r"\{.*\}", reply, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None


def _ai_decide_buy(sym: str, cd: dict, price: float, c: dict):
    """사기 전: 차트·조건·추정 승률을 보고 (살지, 금액 배수 0.25~1, 이유, 차트). 실패·한도 초과면 None → 규칙대로"""
    if not _ai_budget_ok(c):
        return None
    from . import chart
    iv = "1d" if cd["style"] == "swing" else "1m"
    try:
        name = chart.draw(sym, iv, title=sym)
        img = str(chart.path(name))
    except Exception:
        name, img = "", None
    e = cd["est"]
    try:
        from . import weekend
        pat = weekend.pattern_text(_mkt(sym)) if cd["style"] == "surge" else ""
    except Exception:
        pat = ""
    prompt = (lane_persona(cd["style"], _mkt(sym)) + "\n" + (f"{pat}\n" if pat else "") + ""
              f"{'스윙(며칠 보유)' if cd['style'] == 'swing' else '단타(그날 정리)'} 매수 후보 {sym}, 현재가 {price}.\n"
              f"고른 이유: {cd['reason']}\n"
              + (f"최근 공시: {news.note(cd.get('news'))} (급등 이유가 공시로 설명되면 좋은 신호, 이유가 없으면 신중히)\n" if news.note(cd.get('news')) else "")
              + f"학습 기록 추정: 승률 {e['p'] * 100:.0f}% ({e['basis']}), 기대 수익 {e['ev']:+.2f}%\n"
              f"규칙: 손절 -{c['swing_stop_pct' if cd['style'] == 'swing' else 'stop_loss_pct']}%. "
              f"첨부 {'일봉' if iv == '1d' else '1분봉'} 차트(이동평균·VWAP·거래량)를 보고 지금 살지, 산다면 금액을 얼마나(0.25~1) 할지 정하세요. "
              "꼭대기 추격·거래량 감소 하락·지지선 이탈이면 안 사요. 수익률을 최대화하는 쪽으로.\n"
              '형식: {"buy": true/false, "size": 0.25~1, "reason": "한 줄"}')
    r = _ai_json(prompt, img)
    if not r or "buy" not in r:
        return None
    size = max(0.25, min(1.0, float(r.get("size") or 1)))
    return bool(r["buy"]), size, str(r.get("reason", ""))[:140], name


_ai_jobs: dict[tuple, dict] = {}
_ai_wake = {"US": threading.Event(), "KR": threading.Event()}


def _ai_async(key: tuple, fn, *args):
    """AI 판단을 따로(백그라운드) 돌림 → 끝났으면 결과, 아직이면 'PENDING', 실패면 None (규칙대로)"""
    job = _ai_jobs.get(key)
    if job:
        if job["status"] == "pending":
            return "PENDING"
        if time.time() - job["t"] < 180:   # 3분 안의 판단만 씀
            _ai_jobs.pop(key, None)
            return job["result"]
        _ai_jobs.pop(key, None)
    m = cur_market()
    _ai_jobs[key] = {"status": "pending", "t": time.time(), "result": None}

    def run():
        with in_market(m):
            try:
                res = fn(*args)
            except Exception:
                res = None
            _ai_jobs[key] = {"status": "done", "t": time.time(), "result": res}
            _ai_wake[m].set()   # 결과 나오면 바로 한 바퀴 더
    threading.Thread(target=run, daemon=True, name="ai-decide").start()
    return "PENDING"


def _ai_peek(key: tuple):
    job = _ai_jobs.get(key)
    if not job:
        return None
    if job["status"] == "pending":
        return "PENDING"
    if time.time() - job["t"] >= 180:
        _ai_jobs.pop(key, None)
        return None
    return job["result"]


def _ai_hold_due(sym: str, c: dict) -> bool:
    with _lock:
        s = state()
        last = s.setdefault("ai_hold_at", {}).get(sym, 0)
        if time.time() - last < c["ai_hold_check_min"] * 60:
            return False
        s["ai_hold_at"][sym] = time.time(); _save(s)
    return True


def _ai_decide_hold(sym: str, held: dict, price: float, high: float, style: str, rule_note: str, c: dict):
    """보유 중: AI 가 차트 보고 일찍 팔지 (팔기, 이유, 차트). 손절·마감 정리는 규칙이 따로 처리"""
    if not _ai_budget_ok(c):
        return None
    from . import chart
    try:
        name = chart.draw(sym, "1d" if style == "swing" else "1m", title=sym, lines={"산 가격": held["avg"]})
        img = str(chart.path(name))
    except Exception:
        name, img = "", None
    pnl = (price / held["avg"] - 1) * 100 if held["avg"] else 0
    prompt = (lane_persona(style, _mkt(sym)) + "\n"
              f"{'스윙' if style == 'swing' else '단타'} 보유 종목 {sym}: 평단 {held['avg']:g}, 현재가 {price:g} ({pnl:+.1f}%), 산 뒤 최고가 {high:g}.\n"
              f"지금 규칙 판단: {rule_note}\n첨부 차트를 보고 지금 일찍 파는 게 수익률에 나은지 판단하세요. "
              "추세가 살아 있으면 들고 가고, 꺾이는 게 뚜렷하면 팔아요. 애매하면 들고 가요.\n"
              '형식: {"sell": true/false, "reason": "한 줄"}')
    r = _ai_json(prompt, img)
    if not r or "sell" not in r:
        return None
    return bool(r["sell"]), str(r.get("reason", ""))[:140], name


# ── 🧪📼 실험실 ──
def _cross_review(day: str, market: str) -> str:
    """🤝 장 마감 후 두 AI 가 서로의 거래를 복기 → 교훈 2~3개를 공유 노트에 (AI 호출 1번)"""
    from . import providers as pv
    from . import team as tm
    with tl._lock:
        ts = [t for t in tl._load()["trades"] if t["closed"] and t["day"] == day and tl._tm(t) == market]
    if not ts:
        return ""
    def _row(t):
        f = t.get("features") or {}
        return {"sym": t["sym"], "pnl": t["pnl_pct"], "exit": t.get("exit_kind"), "mfe": t["mfe"], "mae": t["mae"],
                "why_buy": (f.get("why_buy") or f.get("ai_decide") or ""), "change": f.get("change"), "ratio": f.get("ratio"),
                "from_high": f.get("from_high"), "pullback5": f.get("pullback5"), "run5": f.get("run5"), "ev": f.get("ev"),
                "mode": t["mode"]}
    by = {LANES[st]: [_row(t) for t in ts if tl._ts(t) == st] for st in LANES}
    c = config()
    prompt = ("두 자동매매 AI 의 오늘 거래예요. " + " / ".join(f"{LANES[st]}: {lane_mode(c, st)}" for st in LANES) + "\n"
              f"{json.dumps(by, ensure_ascii=False)}\n"
              "둘이 서로의 거래를 평가해 주세요: 각자 잘한 점·아쉬운 점, 그리고 상대에게서 배울 점. "
              "결과는 다음에 매수·매도 판단할 때 바로 쓸 수 있는 교훈 2~3개로 (숫자는 위 자료만, 한 줄씩, 투자 권유 문구 없이).\n"
              '형식: {"lessons": ["...", "..."], "swing": "1호에게 한 줄", "surge": "2호에게 한 줄"}')
    try:
        reply = pv.chat(tm.fast_spec(), "너는 두 트레이더의 코치다. 짧고 구체적으로.", [{"role": "user", "content": prompt}], effort="low", timeout=120)
        m = re.search(r"\{.*\}", reply, re.S)
        r = json.loads(m.group(0)) if m else {}
    except Exception as e:
        event("오류", f"교차 복기 AI 실패: {type(e).__name__}")
        return ""
    lessons = [str(x).strip() for x in (r.get("lessons") or []) if str(x).strip()][:3]
    for st in LANES:
        if r.get(st):
            lessons.append(f"[{LANES[st]}에게] {str(r[st]).strip()}")
    for x in lessons:
        tl.add_lesson(market, x, "교차 복기")
    msg = f"{day} 두 AI 교차 복기 — " + " / ".join(lessons[:3])
    event("교차 복기", msg[:400])
    return msg


def lane_auto(market: str) -> list[str]:
    """🤝 ① 2호(가상)가 조건 채우면 실전 ② 실전 AI 가 연속 손실이면 가상으로 ③ 잘하는 쪽으로 예산 몫 5%p (범위 안)"""
    c = config()
    out = []
    with in_market(market):
        s = state()
        days, need = int(c["lane_live_days"]), int(c["lane_live_trades"])
        for st in LANES:
            lm = lane_mode(c, st)
            if c["lane_auto_live"] and lm == "paper" and c["mode"] == "live" and s.get("user_live_ok"):
                stp = tl._stats([t for t in tl.trades_since("paper", (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d"), market) if tl._ts(t) == st])
                if stp.get("n", 0) >= need and stp["avg"] > 0 and stp["win_rate"] >= 45:
                    if save_config({f"lane_{st}_mode": "live"}, _by_auto=True).get("ok"):
                        msg = f"{LANES[st]} 가상 → 실전: 최근 {days}일 가상 {stp['n']}건 승률 {stp['win_rate']}% 평균 {stp['avg']:+.2f}%"
                        event("규칙 교체", msg); out.append(msg)
            elif c["lane_auto_live"] and lm == "live" and c["mode"] == "live":
                sums = _lane_day_sums(market, st, int(c["mode_down_days"]))
                if len(sums) >= int(c["mode_down_days"]) and all(x < 0 for x in sums):   # N일 연속 손실 → 이 AI 만 가상으로
                    if save_config({f"lane_{st}_mode": "paper"}, _by_auto=True).get("ok"):
                        msg = f"{LANES[st]} 실전 → 가상: {len(sums)}일 연속 손실 ({', '.join(f'{x:+.1f}%' for x in sums)}) — 가상에서 회복되면 다시 실전"
                        event("자동 조정", msg); out.append(msg)
        if c["lane_auto_share"] and all(lane_mode(c, st) != "off" for st in LANES):
            a, b = lane_stats(market, "surge", days), lane_stats(market, "swing", days)
            if a.get("n", 0) >= 10 and b.get("n", 0) >= 10 and a["avg"] != b["avg"]:
                cur = float(c["lane_share_surge"])
                new = min(float(c["lane_share_max"]), max(float(c["lane_share_min"]), cur + (5 if a["avg"] > b["avg"] else -5)))
                if new != cur and save_config({"lane_share_surge": new}, _by_auto=True).get("ok"):
                    msg = f"예산 몫 2호 {cur:g}% → {new:g}% (최근 {days}일 평균: 2호 {a['avg']:+.2f}% vs 1호 {b['avg']:+.2f}%)"
                    event("자동 조정", msg); out.append(msg)
    return out


def _apply_params(params: dict, why: str, evidence: str) -> bool:
    """그림자 매매에서 나은 조합 → 실전 설정 (손절은 자동 범위 안으로)"""
    c = config()
    new = {k: v for k, v in params.items() if k in shadow.TUNE_KEYS and c.get(k) != v}
    if "stop_loss_pct" in new:
        new["stop_loss_pct"] = min(c["stop_max"], max(c["stop_min"], float(new["stop_loss_pct"])))
    if "surge_max_change_pct" in new:   # 급등 최대 상승률은 좁히기만 (넓히기 금지)
        new["surge_max_change_pct"] = min(float(c["surge_max_change_pct"]), float(new["surge_max_change_pct"]))
    new = {k: v for k, v in new.items() if c.get(k) != v}
    if not new:
        return False
    r = save_config(new, _by_auto=True)
    if not r.get("ok"):
        return False
    for k, v in new.items():
        tl.record_auto(k, LABELS.get(k, k), c.get(k), v, why, evidence)
    return True


def backtest_now(wait: bool = False) -> dict:
    c = config()
    m = cur_market()

    def job():
        with in_market(m):
            try:
                r = bt.run(c, m, signal_surge, signal_swing, state().get("swing_hist", []), log=_log_fn)
                n = shadow.add_seeds(config(), m, bt.seeds(m)) if c["shadow_on"] else 0
                event("백테스트", r["note"] + (f" · 상위 조합 {n}개를 그림자 매매에 추가" if n else ""))
            except Exception as e:
                event("오류", f"백테스트 실패: {type(e).__name__}: {e}")
    if wait:
        job(); return {"ok": True}
    threading.Thread(target=job, daemon=True, name="backtest").start()
    return {"ok": True, "message": "백테스트 시작 (몇 분)"}


def lab_overview() -> dict:
    c = config()
    return {"shadow": shadow.overview(c, c["market"]) if c["shadow_on"] else {}, "backtest": bt.load(c["market"])}


# ── 🩺 실데이터 점검 (그날 처음 장이 열릴 때 자동 · 화면 버튼) ──
REF_SYMBOL = {"US": "AAPL", "KR": "005930"}


def _calendar_open(market: str, day: str):
    """토스 휴장일 정보 → True(열림)/False(휴장)/None(모름). 응답 형식이 확실치 않아 여러 이름을 확인"""
    try:
        r = toss._get(f"/api/v1/market-calendar/{market}", date=day)
    except toss.TossError:
        return None
    items = r if isinstance(r, list) else [r] if isinstance(r, dict) else []
    for it in items:
        if not isinstance(it, dict):
            continue
        if it.get("date") and str(it["date"])[:10] != day:
            continue
        for k in ("isOpen", "open", "isTradingDay", "tradingDay", "marketOpen", "isBusinessDay", "businessDay"):
            if isinstance(it.get(k), bool):
                return it[k]
        for k in ("isHoliday", "holiday", "closed", "isClosed"):
            if isinstance(it.get(k), bool):
                return not it[k]
    return None


def preflight() -> dict:
    """실제 토스 데이터로 엔진이 기대하는 형식이 맞는지 확인. 중요한 게 틀리면 그날 새로 사지 않음 (보유 종목 관리는 계속)"""
    c = config()
    m = c["market"]
    day = picker.session_date(m)
    checks: list[tuple[str, bool, str, bool]] = []   # (이름, 통과, 설명, 중요)

    def add(name, ok, detail, critical=True):
        checks.append((name, bool(ok), detail, critical))

    closed = False
    if not toss.enabled():
        add("토스 연결", False, "토스 키가 없어요", True)
    else:
        try:
            toss.account_seq(); add("로그인·계좌", True, "종합계좌 찾음")
        except toss.TossError as e:
            add("로그인·계좌", False, str(e))
        op = _calendar_open(m, day)
        if op is False:
            closed = True
        add("휴장일", True, "오늘 휴장 → 매매 안 함" if closed else ("장 열리는 날" if op else "휴장 정보 형식을 몰라서 주말만 쉬어요"), False)
        ref = REF_SYMBOL[m]
        try:
            r = toss._get("/api/v1/candles", symbol=ref, interval="1d", count=5) or {}
            cs = r.get("candles") or []
            bad = []
            for x in cs:   # 엔진은 날짜를 timestamp 앞 10글자로 비교 → 그 시장 날짜와 같아야 함
                ts = datetime.fromisoformat(str(x["timestamp"]).replace("Z", "+00:00"))
                if ts.tzinfo and picker.session_date(m, ts) != str(x["timestamp"])[:10]:
                    bad.append(str(x["timestamp"]))
            add("차트 날짜 기준", bool(cs) and not bad, f"{len(cs)}개 확인" + (f" · 날짜가 어긋나는 봉 {len(bad)}개 (예: {bad[0]})" if bad else ""))
        except (toss.TossError, ValueError, KeyError) as e:
            add("차트 날짜 기준", False, f"일봉을 못 읽었어요: {e}")
        try:
            q = toss.prices([ref]).get(ref) or {}
            ts = q.get("time")
            fresh = True
            mins = minutes_now(m, c)
            if ts and mins is not None and mins >= 5:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)).total_seconds() / 60
                fresh = age < 20
                add("현재가 신선도", fresh, f"{age:.0f}분 전 가격", False)
            else:
                add("현재가", bool(q.get("price")), f"{q.get('price')} (시각 {ts or '없음'})")
        except (toss.TossError, ValueError) as e:
            add("현재가", False, str(e))
        try:
            h = toss.holdings()
            items = h.get("items") or []
            ok = all({"symbol", "quantity", "averagePurchasePrice"} <= set(it) for it in items)
            add("보유 종목 형식", ok, f"보유 {len(items)}개" + ("" if ok else " · 필요한 칸(symbol/quantity/averagePurchasePrice)이 없어요"))
        except toss.TossError as e:
            add("보유 종목 형식", False, str(e))
        try:
            bp = toss.buying_power("KRW" if m == "KR" else "USD") or {}
            float(bp.get("cashBuyingPower"))
            add("주문 가능 현금", True, f"{bp.get('cashBuyingPower')} {'원' if m == 'KR' else '달러'}")
        except (toss.TossError, TypeError, ValueError) as e:
            add("주문 가능 현금", False, f"형식이 달라요: {e}")
        try:
            oo = toss.open_orders()
            add("미체결 주문 형식", isinstance(oo, list), f"{len(oo)}건")
        except toss.TossError as e:
            add("미체결 주문 형식", False, str(e))
    failed = [x for x in checks if x[3] and not x[1]]
    res = {"day": day, "at": datetime.now(KST).strftime("%H:%M"), "closed": closed, "ok": not failed,
           "checks": [{"name": n, "ok": o, "detail": d, "critical": cr} for n, o, d, cr in checks]}
    with _lock:
        s = state(); s["preflight"] = res; _save(s)
    if closed:
        event("휴장", f"{'미장' if m == 'US' else '국장'} 오늘({day}) 휴장 → 매매 안 함")
    elif failed:
        why = "실데이터 점검 실패: " + " · ".join(f"{n}({d})" for n, _, d, _ in failed)
        if c["mode"] == "live":
            _halt_today(why)   # 새로 사지 않음 (보유 종목 손절·정리는 계속)
        event("실데이터", why + (" → 오늘은 새로 사지 않아요" if c["mode"] == "live" else ""))
    else:
        event("실데이터", "점검 통과: " + " · ".join(f"{n}" for n, *_ in checks))
    return res
