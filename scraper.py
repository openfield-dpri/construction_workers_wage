
"""求人ボックスの公開検索結果から、研究用の賃金メタデータを低頻度で収集する。"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, quote, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://xn--pckua2a7gp15o89zb.com"
ROBOTS_URL = f"{BASE_URL}/robots.txt"
SOURCE_SITE = "求人ボックス"
USER_AGENT = "ConstructionWageResearchBot/1.0 (+https://github.com/openfield-dpri/041_construction_worker_price)"
# ネットワーク負荷を抑えるため、HTTP リクエストは短いタイムアウト・少数回の試行に留める。
REQUEST_TIMEOUT_SECONDS = 20
REQUEST_RETRIES = 2
DEFAULT_SLEEP_SECONDS = 4.0
# 詳細ページの確認数は検索ごとに上限で制御する。
MAX_DETAILS_PER_SEARCH = 2

# 日別 CSV の列順。既存ファイルのスキーマ確認にもこの定義を用いる。
COLUMNS = [
    "scrape_date",
    "source_site",
    "query_occupation",
    "prefecture",
    "job_title",
    "company_name",
    "location",
    "work_prefecture",
    "city_ward",
    "oaza_town",
    "chome",
    "ban",
    "go",
    "wage_text",
    "wage_type",
    "wage_min",
    "wage_max",
    "wage_unit",
    "employment_type",
    "posted_date_text",
    "job_url",
    "search_url",
    "is_excluded_fulltime",
    "is_disaster_related",
    "notes",
]

PREFECTURES = (
    "北海道,青森県,岩手県,宮城県,秋田県,山形県,福島県,茨城県,栃木県,群馬県,埼玉県,千葉県,"
    "東京都,神奈川県,新潟県,富山県,石川県,福井県,山梨県,長野県,岐阜県,静岡県,愛知県,"
    "三重県,滋賀県,京都府,大阪府,兵庫県,奈良県,和歌山県,鳥取県,島根県,岡山県,広島県,"
    "山口県,徳島県,香川県,愛媛県,高知県,福岡県,佐賀県,長崎県,熊本県,大分県,宮崎県,"
    "鹿児島県,沖縄県"
).split(",")

OCCUPATION_GROUPS = {
    "工事現場作業員": ["工事現場作業員", "現場作業員", "建設現場作業員", "土工", "普通作業員"],
    "土木作業員": ["土木作業員", "土工", "一般土木", "道路工事", "河川工事", "造成工事"],
    "建設作業員": ["建設作業員", "建築作業員", "建設スタッフ", "建設工"],
    "舗装作業員": ["舗装作業員", "舗装工", "道路舗装", "アスファルト舗装"],
    "重機オペレーター": ["重機オペレーター", "バックホー", "ユンボ", "油圧ショベル", "建設機械オペレーター"],
    "基礎工事": ["基礎工事", "基礎工", "杭工事", "地盤改良"],
    "躯体工事": ["躯体工事", "躯体工", "建方"],
    "解体工事": ["解体工事", "解体工", "解体作業員", "斫り"],
    "設備工事": ["設備工事", "設備工", "設備施工"],
    "電気工事": ["電気工事", "電気工", "電工", "電気設備工事"],
    "空調工事": ["空調工事", "空調設備", "空調工", "ダクト工", "冷媒配管"],
    "水道工事": ["水道工事", "給排水設備", "上下水道工事"],
    "外構工事": ["外構工事", "エクステリア工事", "造成工事"],
    "屋根・外壁塗装": ["塗装工", "建築塗装", "外壁塗装", "屋根塗装"],
    "とび": ["とび", "鳶", "鳶工", "足場鳶", "鉄骨鳶"],
    "足場工": ["足場工", "足場組立", "足場施工"],
    "鉄筋工": ["鉄筋工", "鉄筋施工", "鉄筋組立"],
    "鉄骨工": ["鉄骨工", "鉄骨建方", "鉄骨鳶"],
    "大工": ["大工", "型枠大工", "造作大工", "木造大工", "大工見習"],
    "配管工": ["配管工", "配管工事", "給排水配管", "設備配管", "衛生配管"],
    "内装工": ["内装工", "内装仕上工", "軽天工", "ボード工", "クロス工"],
    "左官": ["左官", "左官工"],
    "防水工": ["防水工", "シーリング", "防水施工"],
    "保温工": ["保温工", "断熱工", "熱絶縁工"],
    "タイル工": ["タイル工", "タイル施工"],
    "ブロック工": ["ブロック工", "コンクリートブロック"],
    "造園師・庭師": ["造園", "庭師", "植木職", "植栽工"],
}
OCCUPATIONS = list(OCCUPATION_GROUPS)

# robots.txt の判定に加えて、検索候補・応募・API などのページはコード上でも取得対象外にする。
BLOCKED_PATH_PREFIXES = (
    "/api/",
    "/suggest/",
    "/rd/",
    "/map/",
    "/my/",
    "/apply",
    "/pdf/",
    "/server-collect/",
    "/jobreport",
)
# 詳細取得を許可するのは、求人ボックス内の公開求人詳細 URL に限る。
PUBLIC_JOB_PATH_PREFIXES = ("/jb/", "/jbi/", "/jbn/")
EXCLUDED_EMPLOYMENT = ("正社員", "新卒", "新卒採用")
NEGATIVE_KEYWORDS = (
    "品出し",
    "レジ",
    "販売",
    "接客",
    "売場",
    "売り場",
    "店舗スタッフ",
    "ショップスタッフ",
    "販売員",
    "商品管理",
    "ドラッグストア",
    "ホームセンター",
    "営業",
    "ルート営業",
    "提案営業",
    "反響営業",
    "不動産営業",
    "住宅営業",
    "事務",
    "一般事務",
    "営業事務",
    "受付",
    "経理",
    "総務",
    "人事",
    "施工管理",
    "現場監督",
    "建築士",
    "設計",
    "CAD",
    "積算",
    "工程管理",
    "品質管理",
    "ドライバー",
    "配送",
    "配達",
    "運転手",
    "倉庫",
    "物流",
    "フォークリフト",
    "ピッキング",
    "製造オペレーター",
    "工場作業",
    "ライン作業",
    "検査員",
    "組立工",
    "警備",
    "清掃",
    "介護",
    "看護",
    "保育",
)
OCCUPATION_NEGATIVE_KEYWORDS = {
    "大工": ("品出し", "販売", "売場", "売り場", "ホームセンター", "営業", "施工管理", "設計", "CAD", "積算"),
}
DISASTER_KEYWORDS = ("災害", "復興", "地震", "震災", "被災", "復旧", "台風", "豪雨", "洪水", "津波", "土砂災害")
LOGGER = logging.getLogger(__name__)


def jst_today() -> str:
    return datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()


@dataclass(frozen=True)
class RobotsPolicy:
    parser: RobotFileParser

    def allows(self, url: str) -> bool:
        parsed = urlparse(url)
        # 外部サイトや HTTPS 以外のリンクへは遷移しない。
        if parsed.scheme != "https" or parsed.netloc != urlparse(BASE_URL).netloc:
            return False
        # robots.txt に記載されない可能性がある高リスクなパスも明示的に遮断する。
        if any(parsed.path.startswith(prefix) for prefix in BLOCKED_PATH_PREFIXES):
            return False
        return self.parser.can_fetch(USER_AGENT, url)


def is_public_job_url(url: str) -> bool:
    """求人ボックス内の公開求人詳細ページだけを詳細取得の対象にする。"""
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == urlparse(BASE_URL).netloc
        and parsed.path.startswith(PUBLIC_JOB_PATH_PREFIXES)
    )


def fetch_robots(session: requests.Session) -> RobotsPolicy:
    """robots.txt を明示的に取得・解析する。失敗時は呼び出し元で収集を中止する。"""
    response = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    parser = RobotFileParser()
    parser.set_url(ROBOTS_URL)
    parser.parse(response.text.splitlines())
    return RobotsPolicy(parser)


def build_search_url(group_name: str, prefecture: str) -> str:
    """雇用形態・掲載日・対象外職種で絞った公開検索 URL を生成する。"""
    excluded_keywords = " ".join(f"not:{keyword}" for keyword in NEGATIVE_KEYWORDS)
    return f"{BASE_URL}/{quote(group_name)}の仕事-{quote(prefecture)}?e=5,3,4&q={quote(excluded_keywords)}"


def clean_text(node: Tag | None) -> str:
    # HTML の改行・連続空白を単一の空白へそろえ、CSV にそのまま書ける短い文字列にする。
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def first_text(card: Tag, selectors: Iterable[str]) -> str:
    # サイト側の CSS クラス変更に備え、候補セレクタを優先順に試す。
    for selector in selectors:
        value = clean_text(card.select_one(selector))
        if value:
            return value
    return ""


def is_excluded(text: str) -> bool:
    # 除外理由は削除せず、CSV のフラグとして後段の分析で扱えるようにする。
    return any(term in text for term in EXCLUDED_EMPLOYMENT)


def has_excluded_keyword(text: str) -> bool:
    """対象外の職種語を含む求人を、保存前に検索結果から除外する。"""
    return any(keyword in text for keyword in NEGATIVE_KEYWORDS)


def matches_occupation_group(
    job_title: str, group_name: str, *additional_texts: str
) -> bool:
    """職種の肯定語を含み、共通・職種別の除外語を含まない求人か判定する。"""
    text = " ".join((job_title, *additional_texts))
    if has_excluded_keyword(text):
        return False
    if any(keyword in text for keyword in OCCUPATION_NEGATIVE_KEYWORDS.get(group_name, ())):
        return False
    return any(keyword in text for keyword in OCCUPATION_GROUPS[group_name])


def is_disaster_related(*texts: str) -> bool:
    return any(keyword in text for text in texts for keyword in DISASTER_KEYWORDS)


def is_probably_ad_or_recommendation(card: Tag) -> bool:
    """広告・関連求人らしいカードを検索結果として扱わない。"""
    text = clean_text(card)
    class_text = " ".join(card.get("class", []))
    ad_keywords = ("スポンサー", "PR", "広告", "おすすめ求人", "関連求人", "あなたにおすすめ")
    return any(keyword in text for keyword in ad_keywords) or bool(
        re.search(r"(ad|sponsor|recommend|related)", class_text, re.IGNORECASE)
    )


def parse_work_address(location: str) -> dict[str, str]:
    """公開求人に明記された日本の勤務地を、推測せず住所要素に分ける。"""
    empty = {
        "work_prefecture": "",
        "city_ward": "",
        "oaza_town": "",
        "chome": "",
        "ban": "",
        "go": "",
    }
    normalized = location.translate(str.maketrans("０１２３４５６７８９－", "0123456789-"))
    prefecture = next((name for name in PREFECTURES if name in normalized), "")
    if not prefecture:
        # 都道府県を特定できない勤務地は、推測で補完しない。
        return empty
    remainder = normalized.split(prefecture, 1)[1].lstrip(" 　:：")
    remainder = re.sub(r"\s+", "", remainder)
    remainder = re.sub(r"(?:駅.*$|徒歩\d+分.*$|車\d+分.*$|バス\d+分.*$)", "", remainder)
    result = {**empty, "work_prefecture": prefecture}

    city_match = re.match(
        r"((?:[^0-9\s-]+?市(?:[^0-9\s-]+?区)?|[^0-9\s-]+?区|[^0-9\s-]+?郡[^0-9\s-]+?[町村]|[^0-9\s-]+?[町村]))",
        remainder,
    )
    if city_match:
        result["city_ward"] = city_match.group(1)
        remainder = remainder[city_match.end():]

    # 「丁目・番地・号」の明示表記を先に抽出する。
    chome_match = re.search(r"(\d+)丁目", remainder)
    ban_match = re.search(r"(\d+)(?:番地|番)", remainder)
    go_match = re.search(r"(\d+)号", remainder)
    result["chome"] = chome_match.group(1) if chome_match else ""
    result["ban"] = ban_match.group(1) if ban_match else ""
    result["go"] = go_match.group(1) if go_match else ""

    # 先頭の番地数字より前を町名として保存する。
    first_number = re.search(r"\d", remainder)
    town_end = first_number.start() if first_number else len(remainder)
    result["oaza_town"] = remainder[:town_end].rstrip(" -　")
    if result["oaza_town"] in ("内", "全域"):
        result["oaza_town"] = ""

    if not (result["chome"] or result["ban"] or result["go"]):
        # 「1-2-3」形式は、明示表記がない場合にだけ丁目-番-号として扱う。
        compact = re.search(r"(\d+)-(\d+)(?:-(\d+))?", remainder)
        if compact:
            result["chome"], result["ban"] = compact.group(1), compact.group(2)
            result["go"] = compact.group(3) or ""
    return result


def normalize_amount(text: str) -> int | None:
    """「1万5,000」「30万」のような日本語金額を円単位に変換する。"""
    text = text.replace(",", "").replace(" ", "").replace("　", "")
    match = re.search(r"(\d+(?:\.\d+)?)万(?:(\d+(?:\.\d+)?))?", text)
    if match:
        return int((float(match.group(1)) * 10000) + float(match.group(2) or 0))
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    return int(float(match.group(1))) if match else None


def parse_wage(wage_text: str) -> dict[str, str]:
    """最初に見つかった給与表記を、原文を残したまま構造化する。"""
    result = {"wage_type": "不明", "wage_min": "", "wage_max": "", "wage_unit": "unknown"}
    # 表示中の給与種別を優先順に確認し、最初に一致した表記だけを採用する。
    unit_map = (("時給", "hour"), ("日給", "day"), ("日額", "day"), ("月給", "month"), ("年収", "year"))
    for label, unit in unit_map:
        match = re.search(rf"{label}\s*([0-9０-９,.万万円]+)(?:\s*(?:～|〜|-|－|〜)\s*([0-9０-９,.万万円]+))?", wage_text)
        if not match:
            continue
        # 正規化できない給与値は空欄のまま残し、推測値を記録しない。
        first = normalize_amount(match.group(1).translate(str.maketrans("０１２３４５６７８９", "0123456789")))
        second = normalize_amount((match.group(2) or "").translate(str.maketrans("０１２３４５６７８９", "0123456789")))
        result.update(
            wage_type=label,
            wage_unit=unit,
            wage_min=str(first) if first is not None else "",
            wage_max=str(second if second is not None else first) if first is not None else "",
        )
        return result
    return result


def extract_job_cards(html: str, search_url: str) -> list[dict[str, str]]:
    """検索結果カードから、求人本文を保存せず公開メタデータだけを抽出する。"""
    soup = BeautifulSoup(html, "html.parser")
    # 検索結果のカードだけを対象にする。関連・類似求人のような別セクションは選択しない。
    cards = soup.select(
        "[class*='jobListItem'], [class*='JobListItem'], article[class*='job'], "
        "[class*='resultArea_item'], [class*='resultAreaItem'], section.p-result_card"
    )
    jobs: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for card in cards:
        if is_probably_ad_or_recommendation(card):
            continue
        # カード内にあるリンクでも、公開求人詳細ページ以外は採用しない。
        title_links = card.select(
            "a.p-result_title_link[href], [class*='jobListItemTitle'] a[href], "
            "[class*='JobListItemTitle'] a[href], [class*='resultArea_title'] a[href], h2 a[href], h3 a[href]"
        )
        link = next(
            (
                candidate
                for candidate in title_links
                if is_public_job_url(urljoin(search_url, str(candidate.get("href"))))
            ),
            None,
        )
        if not link:
            continue
        job_url = urljoin(search_url, str(link.get("href")))
        if job_url in seen_urls:
            # 同一求人が複数の表示枠に出た場合は一度だけ記録する。
            continue
        title = first_text(
            card,
            (
                ".p-result_name",
                "[class*='jobListItemTitle']",
                "[class*='JobListItemTitle']",
                "[class*='resultArea_title']",
                "h2",
                "h3",
                "a",
            ),
        )
        if not title:
            continue
        seen_urls.add(job_url)
        wage_text = first_text(
            card, (".p-result_pay", "[class*='salary']", "[class*='Salary']", "[class*='wage']")
        )
        employment_type = first_text(
            card,
            (
                ".p-result_employType",
                "[class*='employment']",
                "[class*='Employment']",
                "[class*='jobType']",
                "[class*='type']",
            ),
        )
        # 検索結果で公開されるメタデータだけを保存し、求人説明本文は保存しない。
        jobs.append(
            {
                "job_title": title,
                "company_name": first_text(
                    card, (".p-result_companyName", "[class*='company']", "[class*='Company']")
                ),
                "location": first_text(
                    card, (".p-result_area", "[class*='location']", "[class*='Location']", "[class*='place']")
                ),
                "wage_text": wage_text,
                "employment_type": employment_type,
                "posted_date_text": first_text(
                    card, (".p-result_updatedAt_hyphen", "time", "[class*='date']", "[class*='Date']")
                ),
                "job_url": job_url,
                "search_url": search_url,
                "is_disaster_related": str(
                    is_disaster_related(title, wage_text, employment_type, clean_text(card))
                ).lower(),
                "notes": "",
            }
        )
    return jobs


def extract_next_search_page_url(html: str, current_url: str) -> str | None:
    """検索結果の「次へ」リンクだけを抽出し、存在しなければ None を返す。"""
    soup = BeautifulSoup(html, "html.parser")
    candidates = soup.select(
        "a[rel~='next'][href], a[class*='next' i][href], "
        "[class*='pagination' i] a[href], [class*='pager' i] a[href]"
    )
    for link in candidates:
        label = " ".join(
            part for part in (clean_text(link), str(link.get("aria-label", "")), str(link.get("title", ""))) if part
        )
        classes = " ".join(link.get("class", []))
        if not (
            "次へ" in label
            or "次のページ" in label
            or re.search(r"\bnext\b", label, re.IGNORECASE)
            or re.search(r"\bnext\b", classes, re.IGNORECASE)
        ):
            continue
        next_url = urljoin(current_url, str(link["href"]))
        current_parsed = urlparse(current_url)
        current_query = dict(parse_qsl(current_parsed.query, keep_blank_values=True))
        next_parsed = urlparse(next_url)
        next_query = dict(parse_qsl(next_parsed.query, keep_blank_values=True))
        # ページ送りリンクが検索語を落としても、最初の検索条件を維持する。
        if "q" in current_query and "q" not in next_query:
            query = "&".join(
                part for part in current_parsed.query.split("&") if part.split("=", 1)[0] != "page"
            )
            if "page" in next_query:
                query = f"{query}&page={quote(next_query['page'])}"
            next_url = urlunparse(next_parsed._replace(query=query))
        return next_url
    return None


def find_jobposting_jsonld(html: str) -> dict[str, object]:
    """詳細ページ内の JobPosting JSON-LD を探す。見つからなければ空辞書を返す。"""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.select("script[type='application/ld+json']"):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            graph_items = item.get("@graph")
            if isinstance(graph_items, list):
                for graph_item in graph_items:
                    if isinstance(graph_item, dict) and graph_item.get("@type") == "JobPosting":
                        return graph_item
            if item.get("@type") == "JobPosting":
                return item
    return {}


def extract_detail_definition(soup: BeautifulSoup, label: str) -> str:
    """求人詳細の dt ラベルに対応する dd のテキストを取得する。"""
    for dt in soup.select("dt.p-detail_table_title"):
        if clean_text(dt) == label:
            return clean_text(dt.find_next_sibling("dd"))
    return ""


def extract_detail_location(soup: BeautifulSoup) -> str:
    """「勤務地・交通」から住所として表示される先頭行だけを取得する。"""
    for dt in soup.select("dt.p-detail_table_title"):
        if clean_text(dt) != "勤務地・交通":
            continue
        dd = dt.find_next_sibling("dd")
        if dd:
            return clean_text(dd.select_one("p.p-detail_line"))
    return ""


def find_wage_text(text: str) -> str:
    """テキスト全体から最初の給与表記を取得する。"""
    wage_pattern = re.compile(
        r"(?:時給|日給|日額|月給|月額|年収|月収)\s*[0-9０-９,.万万円]+"
        r"(?:\s*(?:～|〜|-|－)\s*[0-9０-９,.万万円]+)?"
    )
    match = wage_pattern.search(text)
    return match.group(0).strip() if match else ""


def extract_detail_wage(soup: BeautifulSoup) -> str:
    """給与関連の詳細項目から給与表記を取得する。"""
    labels = ("給与・報酬", "給与", "報酬", "賃金")
    for dt in soup.select("dt.p-detail_table_title"):
        if not any(label in clean_text(dt) for label in labels):
            continue
        dd = dt.find_next_sibling("dd")
        if not dd:
            continue
        for line in dd.select("p.p-detail_line"):
            wage = find_wage_text(clean_text(line))
            if wage:
                return wage
        wage = find_wage_text(clean_text(dd))
        if wage:
            return wage
    return ""


def extract_detail_employment_type(soup: BeautifulSoup) -> str:
    """「雇用形態」の定義値を取得する。"""
    return extract_detail_definition(soup, "雇用形態")


def extract_detail_job_description(soup: BeautifulSoup) -> str:
    """「仕事内容」を職種判定用にのみ取得し、CSV には保存しない。"""
    return extract_detail_definition(soup, "仕事内容")


def extract_wage_from_job_description(text: str) -> str:
    """仕事内容内の報酬関連見出しブロックから給与表記を取得する。"""
    block_match = re.search(
        r"(?:^|\s)\[(?:報酬|給与|賃金|月収例)\]\s*(.*?)(?=\s*\[[^\]]+\]|$)",
        text,
        re.DOTALL,
    )
    if not block_match:
        return ""

    return find_wage_text(block_match.group(1))


def enrich_from_detail(html: str, job: dict[str, str]) -> None:
    """詳細の定義リストを優先し、JSON-LD で不足する構造化項目を補う。"""
    soup = BeautifulSoup(html, "html.parser")
    location = extract_detail_location(soup)
    if location:
        job["location"] = location

    wage = extract_detail_wage(soup)
    if wage:
        job["wage_text"] = wage

    employment_type = extract_detail_employment_type(soup)
    if employment_type:
        job["employment_type"] = employment_type

    description = extract_detail_job_description(soup)
    if description:
        job["job_description"] = description
        if not job["wage_text"]:
            wage_from_description = extract_wage_from_job_description(description)
            if wage_from_description:
                job["wage_text"] = wage_from_description

    jsonld = find_jobposting_jsonld(html)
    if not jsonld:
        return

    title = jsonld.get("title")
    if isinstance(title, str) and title and not job["job_title"]:
        job["job_title"] = title

    hiring = jsonld.get("hiringOrganization")
    if isinstance(hiring, dict) and not job["company_name"]:
        company_name = hiring.get("name")
        if isinstance(company_name, str):
            job["company_name"] = company_name

    employment = jsonld.get("employmentType")
    if employment and not job["employment_type"]:
        job["employment_type"] = "、".join(map(str, employment)) if isinstance(employment, list) else str(employment)

    date_posted = jsonld.get("datePosted")
    if date_posted and not job["posted_date_text"]:
        job["posted_date_text"] = str(date_posted)

    location = jsonld.get("jobLocation")
    if isinstance(location, list):
        location = location[0] if location else None
    if isinstance(location, dict) and not job["location"]:
        address = location.get("address")
        if isinstance(address, dict):
            parts = (address.get("addressRegion", ""), address.get("addressLocality", ""), address.get("streetAddress", ""))
            job["location"] = " ".join(str(part) for part in parts if part)

    base_salary = jsonld.get("baseSalary")
    if isinstance(base_salary, dict) and not job["wage_text"]:
        value = base_salary.get("value")
        unit_text = base_salary.get("unitText", "")
        if isinstance(value, dict):
            min_value = value.get("minValue")
            max_value = value.get("maxValue")
            if min_value and max_value:
                job["wage_text"] = f"{unit_text} {min_value}～{max_value}".strip()
            elif min_value:
                job["wage_text"] = f"{unit_text} {min_value}".strip()

    jsonld_description = jsonld.get("description")
    if isinstance(jsonld_description, str):
        if not job.get("job_description"):
            job["job_description"] = clean_text(BeautifulSoup(jsonld_description, "html.parser"))
        if is_disaster_related(jsonld_description):
            job["is_disaster_related"] = "true"


def request_allowed_page(
    session: requests.Session, policy: RobotsPolicy, url: str, sleep_seconds: float
) -> str | None:
    """robots.txt 許可済み URL を少数回だけ取得する。404 は再試行せず None を返す。"""
    if not policy.allows(url):
        LOGGER.warning("robots.txt または安全ルールにより取得しません: %s", url)
        return None
    for attempt in range(REQUEST_RETRIES):
        try:
            # 各試行の直前にも待機し、再試行時に連続アクセスしない。
            time.sleep(sleep_seconds)
            response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.text
        except requests.RequestException as error:
            if isinstance(error, requests.HTTPError) and error.response is not None and error.response.status_code == 404:
                LOGGER.warning("取得対象が見つかりません (404): %s", url)
                return None
            LOGGER.warning("取得失敗 (%s/%s): %s", attempt + 1, REQUEST_RETRIES, error)
    return None


def save_rows(rows: list[dict[str, str]], output_path: Path) -> int:
    """日別 CSV 内の重複を除外し、固定列順で保存する。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    key_columns = ("job_url", "scrape_date", "query_occupation", "prefecture")

    def ensure_schema(path: Path) -> None:
        if not path.exists():
            return
        # 既存 CSV を読み直して、列順・UTF-8 BOM の双方を現在の形式へ統一する。
        has_utf8_bom = path.read_bytes().startswith(b"\xef\xbb\xbf")
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            if reader.fieldnames == COLUMNS and has_utf8_bom:
                return
            existing_rows = list(reader)
        # 一時ファイルを置換して、変換途中の CSV を残さない。
        temporary_path = path.with_suffix(f"{path.suffix}.tmp")
        with temporary_path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows({column: row.get(column, "") for column in COLUMNS} for row in existing_rows)
        temporary_path.replace(path)

    def new_rows(path: Path) -> list[dict[str, str]]:
        existing: set[tuple[str, str, str, str]] = set()
        if path.exists():
            # 同日・同検索条件・同一 URL の行は、既に保存済みとみなす。
            with path.open("r", encoding="utf-8-sig", newline="") as file:
                existing = {tuple(row.get(key, "") for key in key_columns) for row in csv.DictReader(file)}
        return [row for row in rows if tuple(row[key] for key in key_columns) not in existing]

    ensure_schema(output_path)
    unique_rows = new_rows(output_path)
    is_new = not output_path.exists()
    # 新規作成時だけ BOM とヘッダーを出力し、Excel などでも日本語列名を読めるようにする。
    encoding = "utf-8-sig" if is_new else "utf-8"
    with output_path.open("a", encoding=encoding, newline="") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerows({column: row.get(column, "") for column in COLUMNS} for row in unique_rows)
    return len(unique_rows)


def collect(
    prefectures: list[str],
    occupations: list[str],
    sleep_seconds: float,
    max_rows: int | None = None,
) -> list[dict[str, str]]:
    if max_rows is not None and max_rows < 1:
        raise ValueError("max_rows は 1 以上にしてください。")
    session = requests.Session()
    # 全リクエストで用途を明示し、日本語の検索結果を優先して受け取る。
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"})
    try:
        policy = fetch_robots(session)
    except requests.RequestException as error:
        raise RuntimeError(f"robots.txt を確認できないため収集を中止しました: {error}") from error

    today = jst_today()
    rows: list[dict[str, str]] = []
    candidate_count = 0
    for prefecture in prefectures:
        for occupation in occupations:
            # 都道府県と職種の組合せごとに、許可された検索結果の全ページをたどる。
            search_url = build_search_url(occupation, prefecture)
            current_url: str | None = search_url
            visited_search_urls: set[str] = set()
            seen_job_urls: set[str] = set()
            detail_count = 0
            while current_url and current_url not in visited_search_urls:
                visited_search_urls.add(current_url)
                html = request_allowed_page(session, policy, current_url, sleep_seconds)
                if html is None:
                    break
                for job in extract_job_cards(html, current_url):
                    candidate_count += 1
                    LOGGER.debug(
                        "抽出候補 %s 件目: title=%s url=%s",
                        candidate_count,
                        job["job_title"],
                        job["job_url"],
                    )
                    if job["job_url"] in seen_job_urls:
                        continue
                    seen_job_urls.add(job["job_url"])
                    if not matches_occupation_group(job["job_title"], occupation):
                        LOGGER.debug("職種ルールに一致しない求人を除外しました: %s", job["job_title"])
                        continue
                    job_url = job["job_url"]
                    robots_allowed = policy.allows(job_url)
                    public_job_url = is_public_job_url(job_url)
                    detail_allowed = robots_allowed and public_job_url
                    LOGGER.debug(
                        "求人詳細URL判定 path=%s robots_allowed=%s public_job_url=%s",
                        urlparse(job_url).path,
                        robots_allowed,
                        public_job_url,
                    )
                    if not detail_allowed:
                        LOGGER.warning(
                            "求人詳細ページは取得しません。ただし検索結果カードの情報は保存します: %s",
                            job_url,
                        )
                        job["notes"] = "求人詳細ページは安全な公開URLと確認できないため取得しませんでした"
                    elif detail_count < MAX_DETAILS_PER_SEARCH:
                        # 上限内の求人だけ詳細を確認し、検索結果で不足する項目を補完する。
                        detail_count += 1
                        detail_html = request_allowed_page(session, policy, job_url, sleep_seconds)
                        if detail_html:
                            enrich_from_detail(detail_html, job)
                            if not matches_occupation_group(
                                job["job_title"],
                                occupation,
                                job["company_name"],
                                job["employment_type"],
                                job.get("job_description", ""),
                            ):
                                LOGGER.debug("詳細ページの職種ルールに一致しない求人を除外しました: %s", job["job_title"])
                                continue
                        else:
                            job["notes"] = "求人詳細ページを取得できませんでした"
                    # 収集済みの原文から分析向けの構造化列を作り、元の給与表記も残す。
                    wage = parse_wage(job["wage_text"])
                    combined = f"{job['job_title']} {job['employment_type']}"
                    rows.append(
                        {
                            **job,
                            **wage,
                            **parse_work_address(job["location"]),
                            "scrape_date": today,
                            "source_site": SOURCE_SITE,
                            "query_occupation": occupation,
                            "prefecture": prefecture,
                            "is_excluded_fulltime": str(is_excluded(combined)).lower(),
                        }
                    )
                    if max_rows is not None and len(rows) >= max_rows:
                        LOGGER.info("テスト取得上限 %s 件に到達したため終了します", max_rows)
                        return rows
                current_url = extract_next_search_page_url(html, current_url)
    return rows


def parse_csv_argument(value: str, allowed: list[str], argument: str) -> list[str]:
    values = [item.strip() for item in value.split(",") if item.strip()]
    # 入力ミスによる無効な検索 URL を防ぐため、定義済み候補だけ受け付ける。
    invalid = sorted(set(values) - set(allowed))
    if invalid:
        raise argparse.ArgumentTypeError(f"{argument} に未対応の値があります: {', '.join(invalid)}")
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description="求人ボックスの公開求人メタデータを低頻度で収集します。")
    parser.add_argument("--prefectures", default=",".join(PREFECTURES), help="カンマ区切りの都道府県名")
    parser.add_argument("--occupations", default=",".join(OCCUPATIONS), help="カンマ区切りの対象職種")
    parser.add_argument("--sleep-seconds", type=float, default=DEFAULT_SLEEP_SECONDS, help="各 HTTP リクエスト前の待機秒数")
    parser.add_argument("--max-rows", type=int, default=None, help="テスト用の最大取得求人件数")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="抽出した各求人カードのタイトル、除外理由、詳細URLの許可判定を出力する",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.sleep_seconds < 3:
        # コマンドライン引数でも最低限の待機時間は下回れない。
        parser.error("--sleep-seconds はサイト負荷を避けるため 3 秒以上にしてください。")
    if args.max_rows is not None and args.max_rows < 1:
        parser.error("--max-rows は 1 以上にしてください。")
    prefectures = parse_csv_argument(args.prefectures, PREFECTURES, "--prefectures")
    occupations = parse_csv_argument(args.occupations, OCCUPATIONS, "--occupations")

    rows = collect(prefectures, occupations, args.sleep_seconds, args.max_rows)
    today = jst_today()
    output_path = Path(f"data/daily/jobs_wage_{today}.csv")
    saved = save_rows(rows, output_path)
    LOGGER.info("%s 件を日別 CSV に保存しました（取得: %s 件、ファイル: %s）。", saved, len(rows), output_path)


if __name__ == "__main__":
    main()
