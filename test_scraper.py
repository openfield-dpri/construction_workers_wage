import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import requests
from scraper import (
    COLUMNS,
    OCCUPATION_GROUPS,
    build_search_url,
    collect,
    enrich_from_detail,
    extract_next_search_page_url,
    extract_detail_definition,
    extract_detail_employment_type,
    extract_detail_job_description,
    extract_detail_location,
    extract_detail_wage,
    extract_wage_from_job_description,
    extract_job_cards,
    find_wage_text,
    has_excluded_keyword,
    matches_occupation_group,
    parse_wage,
    parse_work_address,
    request_allowed_page,
    save_rows,
)
from bs4 import BeautifulSoup


class ExtractJobCardsTests(unittest.TestCase):
    @patch("scraper.time.sleep")
    def test_request_does_not_retry_not_found(self, sleep_mock: Mock) -> None:
        url = "https://xn--pckua2a7gp15o89zb.com/jb/missing-job"
        policy = Mock()
        policy.allows.return_value = True
        response = Mock(status_code=404)
        session = Mock()
        session.get.return_value.raise_for_status.side_effect = requests.HTTPError(response=response)

        self.assertIsNone(request_allowed_page(session, policy, url, sleep_seconds=0))

        session.get.assert_called_once_with(url, timeout=20)
        sleep_mock.assert_called_once_with(0)

    def test_builds_public_search_url_and_matches_group_titles(self) -> None:
        search_url = build_search_url("大工", "東京都")
        self.assertEqual(
            urlparse(search_url).path,
            "/%E5%A4%A7%E5%B7%A5の仕事-%E6%9D%B1%E4%BA%AC%E9%83%BD",
        )
        query = parse_qs(urlparse(search_url).query)
        self.assertEqual(query["e"], ["5,3,4"])
        self.assertNotIn("u", query)
        self.assertIn("not:営業", query["q"][0])
        self.assertIn("not:施工管理", query["q"][0])
        self.assertEqual(list(OCCUPATION_GROUPS), list(dict.fromkeys(OCCUPATION_GROUPS)))
        self.assertTrue(matches_occupation_group("型枠大工（経験者）", "大工"))
        self.assertFalse(matches_occupation_group("建設現場スタッフ", "大工"))
        self.assertTrue(has_excluded_keyword("建設施工管理"))
        self.assertFalse(has_excluded_keyword("型枠大工"))
        self.assertFalse(matches_occupation_group("大工・建設用品売り場の品出しスタッフ", "大工"))
        self.assertFalse(matches_occupation_group("大工経験者向け住宅営業", "大工"))

    def test_extracts_only_the_next_search_result_page(self) -> None:
        current_url = "https://xn--pckua2a7gp15o89zb.com/%E5%A4%A7%E5%B7%A5%E3%81%AE%E4%BB%95%E4%BA%8B-%E6%9D%B1%E4%BA%AC%E9%83%BD?e=5,3,4"
        html = """
        <nav class="pagination">
          <a href="?e=5,3,4&amp;page=1">1</a>
          <a class="pagination_next" href="?e=5,3,4&amp;page=2" aria-label="次へ">次へ</a>
        </nav>
        <a href="/jb/public-job">次へ</a>
        """
        self.assertEqual(
            extract_next_search_page_url(html, current_url),
            "https://xn--pckua2a7gp15o89zb.com/%E5%A4%A7%E5%B7%A5%E3%81%AE%E4%BB%95%E4%BA%8B-%E6%9D%B1%E4%BA%AC%E9%83%BD?e=5,3,4&page=2",
        )
        self.assertIsNone(extract_next_search_page_url("<nav class='pagination'><a href='?page=2'>2</a></nav>", current_url))

    @patch("scraper.request_allowed_page")
    @patch("scraper.fetch_robots")
    def test_collects_recent_jobs_from_every_search_page(
        self, fetch_robots_mock: Mock, request_allowed_page_mock: Mock
    ) -> None:
        search_url = build_search_url("大工", "東京都")
        next_url = f"{search_url}&page=2"
        policy = Mock()
        policy.allows.return_value = True
        fetch_robots_mock.return_value = policy
        request_allowed_page_mock.side_effect = [
            """
            <section class="p-result_card">
              <h2><a class="p-result_title_link" href="/jb/first">大工</a></h2>
              <p class="p-result_updatedAt_hyphen">1日前</p>
            </section>
            <nav class="pagination"><a rel="next" href="?e=5,3,4&page=2">次へ</a></nav>
            """,
            None,
            """
            <section class="p-result_card">
              <h2><a class="p-result_title_link" href="/jb/second">型枠大工</a></h2>
              <p class="p-result_updatedAt_hyphen">7日以内</p>
            </section>
            """,
            None,
        ]

        rows = collect(["東京都"], ["大工"], sleep_seconds=0)

        self.assertEqual([row["job_title"] for row in rows], ["大工", "型枠大工"])
        self.assertEqual(
            [call.args[2] for call in request_allowed_page_mock.call_args_list],
            [
                search_url,
                "https://xn--pckua2a7gp15o89zb.com/jb/first",
                next_url,
                "https://xn--pckua2a7gp15o89zb.com/jb/second",
            ],
        )

    @patch("scraper.request_allowed_page")
    @patch("scraper.fetch_robots")
    def test_collect_stops_at_maximum_rows(
        self, fetch_robots_mock: Mock, request_allowed_page_mock: Mock
    ) -> None:
        search_url = build_search_url("大工", "東京都")
        policy = Mock()
        policy.allows.return_value = True
        fetch_robots_mock.return_value = policy
        request_allowed_page_mock.side_effect = [
            """
            <section class="p-result_card">
              <h2><a class="p-result_title_link" href="/jb/first">大工</a></h2>
              <p class="p-result_updatedAt_hyphen">1日前</p>
            </section>
            <section class="p-result_card">
              <h2><a class="p-result_title_link" href="/jb/second">型枠大工</a></h2>
              <p class="p-result_updatedAt_hyphen">1日前</p>
            </section>
            """,
            None,
        ]

        rows = collect(["東京都"], ["大工"], sleep_seconds=0, max_rows=1)

        self.assertEqual([row["job_title"] for row in rows], ["大工"])
        self.assertEqual([call.args[2] for call in request_allowed_page_mock.call_args_list], [search_url, "https://xn--pckua2a7gp15o89zb.com/jb/first"])

    @patch("scraper.request_allowed_page")
    @patch("scraper.fetch_robots")
    def test_collect_does_not_count_filtered_candidates_toward_maximum_rows(
        self, fetch_robots_mock: Mock, request_allowed_page_mock: Mock
    ) -> None:
        policy = Mock()
        policy.allows.return_value = True
        fetch_robots_mock.return_value = policy
        request_allowed_page_mock.return_value = """
            <section class="p-result_card">
              <h2><a class="p-result_title_link" href="/jb/old">大工</a></h2>
              <p class="p-result_updatedAt_hyphen">8日前</p>
            </section>
            <section class="p-result_card">
              <h2><a class="p-result_title_link" href="/jb/next">大工</a></h2>
              <p class="p-result_updatedAt_hyphen">1日前</p>
            </section>
        """

        rows = collect(["東京都"], ["大工"], sleep_seconds=0, max_rows=1)

        self.assertEqual([row["job_title"] for row in rows], ["大工"])
        self.assertEqual(request_allowed_page_mock.call_count, 2)

    @patch("scraper.request_allowed_page")
    @patch("scraper.fetch_robots")
    def test_collect_logs_each_candidate_title_before_occupation_filter(
        self, fetch_robots_mock: Mock, request_allowed_page_mock: Mock
    ) -> None:
        policy = Mock()
        policy.allows.return_value = True
        fetch_robots_mock.return_value = policy
        request_allowed_page_mock.return_value = """
            <section class="p-result_card">
              <h2><a class="p-result_title_link" href="/jb/unrelated">ゲームテスター</a></h2>
            </section>
        """

        with self.assertLogs("scraper", level="DEBUG") as logs:
            self.assertEqual(collect(["東京都"], ["大工"], sleep_seconds=0), [])

        self.assertIn("抽出候補 1 件目: title=ゲームテスター", "\n".join(logs.output))

    @patch("scraper.request_allowed_page")
    @patch("scraper.fetch_robots")
    def test_collect_follows_all_search_pages_per_condition(
        self, fetch_robots_mock: Mock, request_allowed_page_mock: Mock
    ) -> None:
        search_url = build_search_url("大工", "東京都")
        policy = Mock()
        policy.allows.return_value = True
        fetch_robots_mock.return_value = policy
        request_allowed_page_mock.side_effect = [
            '<nav class="pagination"><a rel="next" href="?e=5,3,4&page=2">次へ</a></nav>',
            '<nav class="pagination"><a rel="next" href="?e=5,3,4&page=3">次へ</a></nav>',
            "",
        ]

        self.assertEqual(collect(["東京都"], ["大工"], sleep_seconds=0), [])
        self.assertEqual(
            [call.args[2] for call in request_allowed_page_mock.call_args_list],
            [search_url, f"{search_url}&page=2", f"{search_url}&page=3"],
        )

    def test_collect_rejects_non_positive_maximum_rows(self) -> None:
        with self.assertRaisesRegex(ValueError, "1 以上"):
            collect(["東京都"], ["大工"], sleep_seconds=0, max_rows=0)

    def test_parse_work_address_keeps_designated_city_ward_together(self) -> None:
        self.assertEqual(
            parse_work_address("熊本県 熊本市 北区"),
            {
                "work_prefecture": "熊本県",
                "city_ward": "熊本市北区",
                "oaza_town": "",
                "chome": "",
                "ban": "",
                "go": "",
            },
        )
        self.assertEqual(
            parse_work_address("東京都 港区 芝"),
            {
                "work_prefecture": "東京都",
                "city_ward": "港区",
                "oaza_town": "芝",
                "chome": "",
                "ban": "",
                "go": "",
            },
        )

    def test_parse_work_address_excludes_station_and_travel_time(self) -> None:
        self.assertEqual(
            parse_work_address("熊本県 熊本市 東区 東海学園前駅車7分"),
            {
                "work_prefecture": "熊本県",
                "city_ward": "熊本市東区",
                "oaza_town": "東海学園前",
                "chome": "",
                "ban": "",
                "go": "",
            },
        )
        self.assertEqual(
            parse_work_address("熊本県 玉東町 木葉駅 車28分"),
            {
                "work_prefecture": "熊本県",
                "city_ward": "玉東町",
                "oaza_town": "木葉",
                "chome": "",
                "ban": "",
                "go": "",
            },
        )

    def test_extracts_current_result_card_fields_and_skips_redirect_links(self) -> None:
        search_url = "https://xn--pckua2a7gp15o89zb.com/%E5%A4%A7%E5%B7%A5%E3%81%AE%E4%BB%95%E4%BA%8B-%E6%9D%B1%E4%BA%AC%E9%83%BD"
        html = """
        <section class="p-result_card">
          <h2 class="p-result_title--ver2">
            <a class="p-result_title_link" href="/jb/public-job"> <span class="p-result_name">大工</span> </a>
          </h2>
          <p class="p-result_companyName">株式会社テスト</p>
          <ul>
            <li class="p-result_area">東京都品川区</li>
            <li class="p-result_pay">月給30万円～45万円 / 賞与あり</li>
            <li class="p-result_employType">業務委託</li>
          </ul>
          <p class="p-result_updatedAt_hyphen">1時間前</p>
        </section>
        <section class="p-result_card">
          <h2><a class="p-result_title_link" href="/rd/?opaque-token">除外する求人</a></h2>
          <li class="p-result_pay">日給1万3,000円～</li>
        </section>
        """

        jobs = extract_job_cards(html, search_url)

        self.assertEqual(
            jobs,
            [
                {
                    "job_title": "大工",
                    "company_name": "株式会社テスト",
                    "location": "東京都品川区",
                    "wage_text": "月給30万円～45万円 / 賞与あり",
                    "employment_type": "業務委託",
                    "posted_date_text": "1時間前",
                    "job_url": "https://xn--pckua2a7gp15o89zb.com/jb/public-job",
                    "search_url": search_url,
                    "is_disaster_related": "false",
                    "notes": "",
                }
            ],
        )
        self.assertEqual(
            parse_wage(jobs[0]["wage_text"]),
            {"wage_type": "月給", "wage_min": "300000", "wage_max": "450000", "wage_unit": "month"},
        )

    def test_flags_disaster_keywords_from_card_and_detail(self) -> None:
        search_url = "https://xn--pckua2a7gp15o89zb.com/search"
        html = """
        <section class="p-result_card">
          <h2><a class="p-result_title_link" href="/jb/public-job">震災復興工事の大工</a></h2>
        </section>
        """

        job = extract_job_cards(html, search_url)[0]
        self.assertEqual(job["is_disaster_related"], "true")

        job["is_disaster_related"] = "false"
        enrich_from_detail(
            """
            <script type="application/ld+json">
            {"@type": "JobPosting", "description": "地震被害の復旧工事を担当します。"}
            </script>
            """,
            job,
        )
        self.assertEqual(job["is_disaster_related"], "true")

    def test_detail_definition_values_override_search_card_values(self) -> None:
        soup = BeautifulSoup(
            """
            <dl>
              <dt class="p-detail_table_title">勤務地・交通</dt>
              <dd>
                <p class="p-detail_line">熊本県熊本市東区○○</p>
                <h3 class="p-detail_subTitle">交通手段・勤務地補足</h3>
                <p class="p-detail_line">東海学園前駅 車7分</p>
              </dd>
              <dt class="p-detail_table_title">給与・報酬</dt>
              <dd>
                <p class="p-detail_line">時給1100円～1300円</p>
                <h3>給与補足</h3>
                <p>賞与あり</p>
              </dd>
              <dt class="p-detail_table_title">雇用形態</dt>
              <dd>アルバイト・パート</dd>
              <dt class="p-detail_table_title">仕事内容</dt>
              <dd>木造住宅の造作大工を担当します。</dd>
            </dl>
            """,
            "html.parser",
        )
        self.assertEqual(extract_detail_location(soup), "熊本県熊本市東区○○")
        self.assertEqual(extract_detail_wage(soup), "時給1100円～1300円")
        self.assertEqual(find_wage_text("月給 25万円～35万円 賞与あり"), "月給 25万円～35万円")
        self.assertEqual(extract_detail_definition(soup, "雇用形態"), "アルバイト・パート")
        self.assertEqual(extract_detail_employment_type(soup), "アルバイト・パート")
        self.assertEqual(extract_detail_job_description(soup), "木造住宅の造作大工を担当します。")

        job = {
            "job_title": "",
            "company_name": "",
            "location": "熊本県 熊本市 東区 東海学園前駅 車7分",
            "wage_text": "時給1100円～",
            "employment_type": "派遣社員",
            "posted_date_text": "",
            "is_disaster_related": "false",
            "notes": "",
        }
        enrich_from_detail(str(soup), job)
        self.assertEqual(job["location"], "熊本県熊本市東区○○")
        self.assertEqual(job["wage_text"], "時給1100円～1300円")
        self.assertEqual(job["employment_type"], "アルバイト・パート")
        self.assertEqual(job["job_description"], "木造住宅の造作大工を担当します。")
        self.assertEqual(job["notes"], "")

    def test_extracts_wage_from_detail_without_a_detail_line(self) -> None:
        soup = BeautifulSoup(
            """
            <dl>
              <dt class="p-detail_table_title">基本給与</dt>
              <dd>月給 25万円～35万円 賞与あり</dd>
            </dl>
            """,
            "html.parser",
        )

        self.assertEqual(extract_detail_wage(soup), "月給 25万円～35万円")

    def test_extracts_wage_embedded_in_job_description(self) -> None:
        description = (
            "[仕事内容] 建設現場での作業を担当します。 "
            "[報酬] 日額9,000〜20,000円 ※能力・経験に応じて決定します "
            "[勤務時間] 8:00〜17:00"
        )
        self.assertEqual(extract_wage_from_job_description(description), "日額9,000〜20,000円")

        job = {
            "job_title": "建設作業員",
            "company_name": "株式会社テスト",
            "location": "",
            "wage_text": "",
            "employment_type": "",
            "posted_date_text": "",
            "is_disaster_related": "false",
            "notes": "",
        }
        enrich_from_detail(
            f"<dl><dt class='p-detail_table_title'>仕事内容</dt><dd>{description}</dd></dl>",
            job,
        )

        self.assertEqual(job["wage_text"], "日額9,000〜20,000円")
        self.assertEqual(parse_wage(job["wage_text"]), {
            "wage_type": "日額",
            "wage_min": "9000",
            "wage_max": "20000",
            "wage_unit": "day",
        })

    def test_rejects_jobs_with_excluded_words_in_detail_description(self) -> None:
        job = {
            "job_title": "大工スタッフ",
            "company_name": "株式会社テスト",
            "location": "",
            "wage_text": "",
            "employment_type": "アルバイト",
            "posted_date_text": "",
            "is_disaster_related": "false",
            "notes": "",
        }
        enrich_from_detail(
            "<dl><dt class='p-detail_table_title'>仕事内容</dt>"
            "<dd>ホームセンター木材コーナーでの品出しを担当します。</dd></dl>",
            job,
        )

        self.assertFalse(
            matches_occupation_group(
                job["job_title"],
                "大工",
                job["company_name"],
                job["employment_type"],
                job["job_description"],
            )
        )

    def test_enriches_from_jobposting_jsonld_when_detail_definitions_are_missing(self) -> None:
        job = {
            "job_title": "",
            "company_name": "",
            "location": "",
            "wage_text": "",
            "employment_type": "",
            "posted_date_text": "",
            "is_disaster_related": "false",
            "notes": "",
        }

        enrich_from_detail(
            """
            <main>関連求人: 月給99万円 地震復旧工事</main>
            <script type="application/ld+json">
            {
              "@type": "JobPosting",
              "title": "大工",
              "hiringOrganization": {"name": "株式会社テスト"},
              "employmentType": ["CONTRACTOR", "FULL_TIME"],
              "datePosted": "2026-08-08",
              "jobLocation": {"address": {
                "addressRegion": "東京都",
                "addressLocality": "品川区",
                "streetAddress": "東五反田1-2-3"
              }},
              "baseSalary": {"unitText": "月給", "value": {"minValue": 300000, "maxValue": 450000}}
            }
            </script>
            """,
            job,
        )

        self.assertEqual(job["job_title"], "大工")
        self.assertEqual(job["company_name"], "株式会社テスト")
        self.assertEqual(job["location"], "東京都 品川区 東五反田1-2-3")
        self.assertEqual(job["wage_text"], "月給 300000～450000")
        self.assertEqual(job["employment_type"], "CONTRACTOR、FULL_TIME")
        self.assertEqual(job["posted_date_text"], "2026-08-08")
        self.assertEqual(job["is_disaster_related"], "false")

        no_jsonld_job = {**job, "wage_text": "", "is_disaster_related": "false", "notes": ""}
        enrich_from_detail("<main>関連求人: 月給99万円 地震復旧工事</main>", no_jsonld_job)
        self.assertEqual(no_jsonld_job["wage_text"], "")
        self.assertEqual(no_jsonld_job["is_disaster_related"], "false")
        self.assertEqual(no_jsonld_job["notes"], "")

    def test_skips_advertisement_or_recommendation_cards(self) -> None:
        search_url = "https://xn--pckua2a7gp15o89zb.com/search"
        html = """
        <section class="p-result_card">
          <h2><a class="p-result_title_link" href="/jb/public-job">通常求人</a></h2>
        </section>
        <section class="p-result_card recommendation">
          <h2><a class="p-result_title_link" href="/jb/recommended-job">おすすめ求人</a></h2>
        </section>
        """

        self.assertEqual([job["job_title"] for job in extract_job_cards(html, search_url)], ["通常求人"])

    def test_save_rows_writes_excel_compatible_utf8_csv(self) -> None:
        row = {column: "" for column in COLUMNS}
        row.update(
            scrape_date="2026-08-08",
            source_site="求人ボックス",
            query_occupation="大工",
            prefecture="東京都",
            job_title="大工",
            job_url="https://example.com/jb/public-job",
        )
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "daily" / "jobs_wage_2026-08-08.csv"

            self.assertEqual(save_rows([row], output_path), 1)
            later_row = {**row, "job_url": "https://example.com/jb/another-public-job"}
            self.assertEqual(save_rows([later_row], output_path), 1)

            self.assertEqual(output_path.read_bytes()[:3], b"\xef\xbb\xbf")
            with output_path.open(encoding="utf-8-sig", newline="") as file:
                saved_rows = list(csv.DictReader(file))
            self.assertEqual([saved_row["job_title"] for saved_row in saved_rows], ["大工", "大工"])


if __name__ == "__main__":
    unittest.main()
