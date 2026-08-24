#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
의약품안전나라 허가/취하 목록 - 성분명(부분일치) 트래킹

대상: https://nedrug.mfds.go.kr/pbp/CCBAE01  (1페이지만 확인)
표  : div#durItemList > table.tb_list
컬럼: 순번 | 제품명 | 업체명 | 허가일자 | 취소/취하일자 | 전문/일반

- 제품명에 키워드가 부분일치로 포함된 품목만 알림 (예: 페라미비르)
- itemSeq 기준으로 기억 → 같은 품목은 두 번 알리지 않음

환경변수
  KEYWORDS      쉼표 구분 (예: 페라미비르,오셀타미비르)
  MATCH_FIELD   product(기본) | company | all
  SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/MAIL_TO
  TG_TOKEN/TG_CHAT_ID        (선택) 텔레그램
  ALERT_ON_CANCEL=0          허가 후 '취하'로 바뀌어도 다시 알리지 않음 (기본 1=알림)
  NOTIFY_ON_FIRST_RUN=1      첫 실행에도 알림
  DEBUG=1                    파싱 결과만 출력, 알림/저장 없음
"""

import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr

import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
BASE = "https://nedrug.mfds.go.kr"
TARGET_URL = os.getenv("TARGET_URL", f"{BASE}/pbp/CCBAE01")
STATE_PATH = os.getenv("STATE_PATH", "state/seen.json")
MAX_KEEP = 20000
DEBUG = os.getenv("DEBUG") == "1"
ALERT_ON_CANCEL = os.getenv("ALERT_ON_CANCEL", "1") == "1"
# 저장소 활동 유지용: 이 일수마다 기록 파일에 날짜를 찍어 커밋을 발생시킴
HEARTBEAT_DAYS = int(os.getenv("HEARTBEAT_DAYS", "14"))
# 생존 확인 메일: 이 일수마다 "정상 작동 중" 메일 발송 (0이면 끔)
ALIVE_MAIL_DAYS = int(os.getenv("ALIVE_MAIL_DAYS", "7"))

COLS = ["no", "product", "company", "permit_date", "cancel_date", "class"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}


def log(*a):
    print(f"[{datetime.now(KST):%Y-%m-%d %H:%M:%S}]", *a, flush=True)


def norm(s: str) -> str:
    """부분일치 비교용 정규화: 소문자 + 공백/하이픈/가운뎃점 제거."""
    return re.sub(r"[\s\-·・]", "", (s or "").lower())


# ------------------------------------------------------------------ fetch
def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  fetch 실패 {i+1}/{tries}: {e}")
            time.sleep(4 * (i + 1))
    raise RuntimeError(f"페이지 요청 실패: {last}")


# ------------------------------------------------------------------ parse
def find_table(soup):
    holder = soup.find(id="durItemList")
    if holder and holder.find("table"):
        return holder.find("table")
    t = soup.select_one("table.tb_list")
    if t:
        return t
    tables = soup.find_all("table")
    return max(tables, key=lambda x: len(x.find_all("tr"))) if tables else None


def parse_rows(html):
    soup = BeautifulSoup(html, "html.parser")
    table = find_table(soup)
    if table is None:
        return []

    body = table.find("tbody") or table
    out = []
    for tr in body.find_all("tr"):
        if tr.find("th"):
            continue
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        vals = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in tds]
        it = dict(zip(COLS, vals))
        for c in COLS:
            it.setdefault(c, "")
        it["cancel_date"] = "" if it["cancel_date"] in ("-", "&nbsp;") else it["cancel_date"]

        item_seq = ""
        for a in tr.find_all("a"):
            m = re.search(r"itemSeq=(\d+)",
                          f"{a.get('href') or ''} {a.get('onclick') or ''}")
            if m:
                item_seq = m.group(1)
                break

        it["item_seq"] = item_seq
        it["url"] = (f"{BASE}/pbp/CCBBB01/getItemDetail?itemSeq={item_seq}"
                     if item_seq else TARGET_URL)

        # 중복 방지 키: itemSeq 하나당 1회.
        # ALERT_ON_CANCEL이면 '허가'와 '취하'를 각각 한 번씩만 알림.
        base_key = item_seq or "x:" + norm(it["product"] + it["company"] + it["permit_date"])
        it["key"] = base_key + ("|C" if (ALERT_ON_CANCEL and it["cancel_date"]) else "")
        it["blob"] = " ".join(vals)
        out.append(it)
    return out


# ------------------------------------------------------------------ state
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("seen", [])
    return st


def save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    # 순서 유지하며 중복 제거 후 최근 것만 보관
    seen, dedup = set(), []
    for k in st["seen"]:
        if k not in seen:
            seen.add(k)
            dedup.append(k)
    st["seen"] = dedup[-MAX_KEEP:]

    new_text = json.dumps(st, ensure_ascii=False, indent=1)
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            if f.read() == new_text:
                log("기록 파일 변경 없음 → 커밋 안 함")
                return
    except FileNotFoundError:
        pass
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        f.write(new_text)
    log("기록 파일 갱신")


def days_since(iso_date):
    """저장된 날짜로부터 며칠 지났는지. 값이 없으면 아주 큰 수."""
    if not iso_date:
        return 10 ** 6
    try:
        d = datetime.strptime(iso_date[:10], "%Y-%m-%d").date()
    except ValueError:
        return 10 ** 6
    return (datetime.now(KST).date() - d).days


# ------------------------------------------------------------------ notify
def send_mail(subject, body):
    user, pw = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS")
    if not (user and pw):
        log("SMTP 미설정 → 메일 생략")
        return
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    to = [x.strip() for x in os.getenv("MAIL_TO", user).split(",") if x.strip()]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("의약품 허가 모니터", user))
    msg["To"] = ", ".join(to)
    msg.set_content(body)

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as sv:
            sv.login(user, pw)
            sv.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as sv:
            sv.starttls()
            sv.login(user, pw)
            sv.send_message(msg)
    log(f"메일 발송 완료 → {to}")


def send_telegram(text):
    token, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT_ID")
    if not (token and chat):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text[:4000],
                  "disable_web_page_preview": True},
            timeout=20,
        ).raise_for_status()
        log("텔레그램 발송 완료")
    except Exception as e:  # noqa: BLE001
        log(f"텔레그램 실패: {e}")


def target_text(it, field):
    if field == "company":
        return it["company"]
    if field == "all":
        return it["blob"]
    return it["product"]


def format_item(it, matched):
    kind = "취하/취소" if it["cancel_date"] else "허가"
    return (
        f"■ {it['product']}\n"
        f"   업체: {it['company']}  |  {it['class']}  |  구분: {kind}\n"
        f"   허가일: {it['permit_date'] or '-'}"
        + (f"  |  취소/취하일: {it['cancel_date']}" if it["cancel_date"] else "")
        + "\n"
        + (f"   ▸ 키워드: {', '.join(matched)}\n" if matched else "")
        + f"   {it['url']}\n"
    )


# ------------------------------------------------------------------ main
def main():
    keywords = [k.strip() for k in os.getenv("KEYWORDS", "").split(",") if k.strip()]
    field = os.getenv("MATCH_FIELD", "product")
    log(f"키워드: {keywords or '(지정 없음 → 전부)'} / 매칭범위: {field}")

    rows = parse_rows(fetch(TARGET_URL))
    log(f"1페이지에서 {len(rows)}건 파싱")

    if DEBUG:
        for r in rows:
            print(json.dumps(
                {k: r[k] for k in ("item_seq", "product", "company",
                                   "permit_date", "cancel_date", "class")},
                ensure_ascii=False))
        if keywords:
            hit = [r["product"] for r in rows
                   if any(norm(k) in norm(target_text(r, field)) for k in keywords)]
            print(f"\n키워드 매칭: {hit or '없음'}")
        print(f">>> 총 {len(rows)}건. 실제 목록과 대조해 보세요.")
        return 0

    if not rows:
        raise RuntimeError("목록을 한 건도 못 읽었습니다. DEBUG=1로 확인하세요.")

    st = load_state()
    seen = set(st["seen"])
    first_run = not seen

    new_items = [r for r in rows if r["key"] not in seen]
    log(f"처음 보는 항목 {len(new_items)}건")

    hits = []
    for it in new_items:
        if not keywords:
            hits.append((it, []))
            continue
        text = norm(target_text(it, field))
        m = [k for k in keywords if norm(k) in text]
        if m:
            hits.append((it, m))

    if first_run and os.getenv("NOTIFY_ON_FIRST_RUN") != "1":
        log("첫 실행 → 기준선만 저장하고 알림 생략")
        hits = []

    if hits:
        body = (
            f"등록된 품목 {len(hits)}건이 키워드와 일치합니다.\n"
            f"확인 시각: {datetime.now(KST):%Y-%m-%d %H:%M} KST\n"
            f"목록: {TARGET_URL}\n\n"
            + "\n".join(format_item(it, m) for it, m in hits)
        )
        subject = f"[의약품] {hits[0][0]['product'][:35]}" + (
            f" 외 {len(hits)-1}건" if len(hits) > 1 else "")
        send_mail(subject, body)
        send_telegram(subject + "\n\n" + body)
        log(f"알림 {len(hits)}건 발송")
    else:
        log("알림 대상 없음")

    # 키워드에 안 걸린 항목도 '본 것'으로 기록 → 다음에 다시 검사하지 않음
    st["seen"] = list(st["seen"]) + [r["key"] for r in new_items]
    today = datetime.now(KST).date().isoformat()
    if new_items:
        st["last_new_at"] = today

    # --- 생존 확인 메일: 일정 주기로 "정상 작동 중" 통지 ---
    if ALIVE_MAIL_DAYS > 0 and days_since(st.get("last_alive_mail")) >= ALIVE_MAIL_DAYS:
        last_hit = st.get("last_alert_at") or "아직 없음"
        alive = (
            f"모니터링이 정상 작동 중입니다.\n\n"
            f"확인 시각   : {datetime.now(KST):%Y-%m-%d %H:%M} KST\n"
            f"감시 키워드 : {', '.join(keywords) or '(전체)'}\n"
            f"현재 목록   : {len(rows)}건\n"
            f"기억 중인 품목: {len(st['seen'])}건\n"
            f"마지막 알림 : {last_hit}\n\n"
            f"이 메일이 {ALIVE_MAIL_DAYS}일 넘게 오지 않으면 모니터링이 멈춘 것이니\n"
            f"GitHub의 Actions 탭을 확인하세요.\n{TARGET_URL}"
        )
        send_mail("[의약품] 모니터링 정상 작동 중", alive)
        st["last_alive_mail"] = today
        log("생존 확인 메일 발송")

    if hits:
        st["last_alert_at"] = today

    # --- 활동 유지: 저장소가 60일 무활동으로 정지되지 않도록 날짜를 갱신 ---
    if days_since(st.get("heartbeat")) >= HEARTBEAT_DAYS:
        st["heartbeat"] = today
        log(f"활동 유지용 날짜 갱신 ({today})")

    save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
