"""Checks on the parts most likely to break quietly: what counts as relevant, how stories are grouped,
the daily key developments, the Parliament data, the health checks, and the page's basic structure."""
import os
import struct
import sys
import tempfile
import time
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
os.environ.pop("ANTHROPIC_API_KEY", None)   # tests never call Claude

import committee_mail as C  # noqa: E402
import health  # noqa: E402
import keystones as K  # noqa: E402
import news_alerts as N  # noqa: E402
import parliament as P  # noqa: E402

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=ZoneInfo("America/Toronto")).timestamp()


def item(title, desc="", source="CBC"):
    return {"title": title, "desc": desc, "source": source, "ts": NOW, "guid": title, "link": "https://x/" + title[:8]}


class Relevance(unittest.TestCase):
    def assoc(self, title, desc=""):
        t = N.classify(item(title, desc))
        return t["assoc"] if t else None

    def test_cada_stories(self):
        self.assertEqual(self.assoc("Canadian Automobile Dealers Association warns on EV mandate"), "CADA")
        self.assertEqual(self.assoc("Canada and U.S. near deal on auto tariffs under CUSMA"), "CADA")
        self.assertEqual(self.assoc("Stellantis to idle Brampton assembly plant in Ontario"), "CADA")

    def test_heal_stories(self):
        self.assertEqual(self.assoc("Ottawa unveils national pharmacare funding deal with provinces"), "HEAL")
        self.assertEqual(self.assoc("Organizations for Health Action urges federal action on health workforce"), "HEAL")

    def test_new_associations(self):
        self.assertEqual(self.assoc("Canada's counter-tariffs hit RV parts as dealers seek remission"), "RVDA")
        self.assertEqual(self.assoc("Parks Canada raises camping fees for 2027"), "RVDA")
        self.assertEqual(self.assoc("Ottawa extends heat pump rebate program for oil-heated homes"), "HRAI")
        self.assertEqual(self.assoc("HRAI warns new refrigerant rules will raise costs in Canada"), "HRAI")
        self.assertEqual(self.assoc("Downtown Toronto office vacancy hits record as federal workers stay home"), "IDA")
        self.assertEqual(self.assoc("Vancouver encampment ruling puts pressure on B.C. cities"), "IDA")

    def test_common_words_in_the_body_do_not_move_a_story(self):
        # "ventilation" and "downtown" in the body must not pull a health story to HRAI or IDA.
        self.assertEqual(self.assoc("Ottawa unveils national pharmacare funding deal with provinces",
                                    "Patients wait in a downtown Toronto hospital where ventilation was upgraded."), "HEAL")
        self.assertEqual(self.assoc("Canada and U.S. near deal on auto tariffs under CUSMA",
                                    "Talks were held at a downtown Ottawa hotel."), "CADA")

    def test_every_association_has_an_icon_and_topics(self):
        self.assertEqual(set(N.ASSOC_ORDER), set(N.ASSOCS))
        for a in N.ASSOC_ORDER:
            self.assertTrue(any(t["assoc"] == a for t in N.TOPICS), a)
        self.assertEqual(len({t["key"] for t in N.TOPICS}), len(N.TOPICS))

    def test_unrelated_story_is_dropped(self):
        self.assertIsNone(self.assoc("Local bakery wins award for best croissant"))

    def test_outlet_name_alone_does_not_make_a_story_relevant(self):
        self.assertIsNone(N.classify(item("Best hiking boots of the year", source="Automotive News Canada")))

    def test_trusted_outlets(self):
        for name in ("CBC", "Reuters", "The Globe and Mail", "Parliament of Canada (LEGISinfo)"):
            self.assertTrue(N.trusted_record({"source": name}), name)
        for name in ("kendranewsaustralia.com.au", "Foro3D"):
            self.assertFalse(N.trusted_record({"source": name}), name)


class Grouping(unittest.TestCase):
    STORY = {"story": "Canada-U.S. CUSMA trade talks", "assoc": "CADA",
             "titles": [("U.S. trade chief says Canada tariff deal is close", 0), ("Ford will not back a U.S.-Canada deal", 0)]}

    def art(self, title, assoc="CADA"):
        return {"title": title, "desc": "", "source": "x", "topic": {"assoc": assoc}}

    def test_same_matter_joins(self):
        self.assertTrue(N.fits_story(self.art("LeBlanc heads to Washington for CUSMA talks"), self.STORY))

    def test_shared_broad_subject_does_not_join(self):
        self.assertFalse(N.fits_story(self.art("Canada and China extend trade deals as Xi meets Carney"), self.STORY))

    def test_other_association_does_not_join(self):
        self.assertFalse(N.fits_story(self.art("CUSMA talks and drug prices", assoc="HEAL"), self.STORY))


class Keystones(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "news.json")

    def art(self, n, title, source="CBC", at=NOW - 3600):
        return {"id": f"a{n}", "thread": "T1", "story": "Auto tariff talks", "assoc": "CADA", "topic": "Trade/CUSMA",
                "level": 3, "title": title, "source": source, "link": f"https://x/{n}", "published": at, "seen": at,
                "summary": ""}

    def run_pass(self, news, answer, now=NOW):
        calls = []

        def ask(story, fresh, _now):
            calls.append([a["id"] for a in fresh])
            return answer
        K.update(news, self.path, now, ask, trusted=N.trusted_record)
        return calls

    FACT = {"date": "2026-11-23", "part": "Autos", "replaces": [],
            "text": "Canada and the U.S. agreed to cut the auto tariff from 25 to 10 percent effective January 1."}

    def test_adds_cited_fact_and_rejects_bad_ones(self):
        news = {"items": [self.art(1, "Tariff cut agreed"), self.art(2, "Reaction"), self.art(3, "Analysis")]}
        answer = {"stands": "Canada and the U.S. have agreed to lower the auto tariff starting in January.", "confirm": [], "add": [
            dict(self.FACT, articles=[0]),
            dict(self.FACT, articles=[0], text="Canada and U.S. agreed to cut auto tariff from 25 to 10 percent, effective January 1."),
            {"date": "2026-11-23", "part": "Steel", "replaces": [], "articles": [99],
             "text": "A claim with no article behind it should never be recorded at all."}]}
        self.run_pass(news, answer)
        keys = news["stories"]["T1"]["keys"]
        self.assertEqual(len(keys), 1)                       # the duplicate and the uncited one are gone
        self.assertEqual(keys[0]["src"][0]["source"], "CBC")
        self.assertFalse(keys[0]["prov"])

    def test_runs_once_a_day_and_only_on_new_articles(self):
        news = {"items": [self.art(1, "a"), self.art(2, "b"), self.art(3, "c")]}
        empty = {"stands": "", "add": [], "confirm": []}
        self.assertEqual(len(self.run_pass(news, empty)), 1)
        self.assertEqual(len(self.run_pass(news, empty, NOW + 600)), 0)
        news["items"].append(self.art(4, "d", at=NOW + 80000))
        self.assertEqual(self.run_pass(news, empty, NOW + 86400), [["a4"]])

    def test_untrusted_fact_is_provisional_then_confirmed(self):
        news = {"items": [self.art(1, "x", "Foro3D"), self.art(2, "y", "Foro3D"), self.art(3, "z", "Foro3D")]}
        self.run_pass(news, {"stands": "", "confirm": [], "add": [dict(self.FACT, articles=[0])]})
        self.assertTrue(news["stories"]["T1"]["keys"][0]["prov"])
        news["items"].append(self.art(4, "Tariff cut confirmed", "Reuters", at=NOW + 80000))
        self.run_pass(news, {"stands": "", "add": [], "confirm": [{"key": "k1", "articles": [0]}]}, NOW + 86400)
        key = news["stories"]["T1"]["keys"][0]
        self.assertFalse(key["prov"])
        self.assertEqual(key["src"][0]["source"], "Reuters")

    def test_unconfirmed_fact_expires(self):
        news = {"items": [self.art(1, "x", "Foro3D"), self.art(2, "y", "Foro3D"), self.art(3, "z", "Foro3D")]}
        self.run_pass(news, {"stands": "", "confirm": [], "add": [dict(self.FACT, articles=[0])]})
        self.run_pass(news, {"stands": "", "add": [], "confirm": []}, NOW + 49 * 3600)
        self.assertNotIn("T1", news["stories"])

    def test_claude_failure_loses_nothing(self):
        news = {"items": [self.art(1, "a"), self.art(2, "b"), self.art(3, "c")]}

        def broken(*_):
            raise RuntimeError("down")
        for n in range(4):
            K.update(news, self.path, NOW + n * 300, broken)
        story = K.load(K.stories_path(self.path))["stories"]["T1"]
        self.assertEqual(story["digested"], {})
        self.assertEqual(story["tries"], K.TRIES_PER_DAY)


class Parliament(unittest.TestCase):
    def test_agenda_is_pulled_from_a_notice(self):
        page = """<html><head><title>x</title></head><body><nav><a>Menu</a></nav>
        <p>NOTICE OF MEETING</p><p>Standing Committee on Health (HESA)</p><p>45th Parliament, 1st Session</p>
        <p>Meeting 46</p><p>Wednesday, November 25, 2026, 4:30 p.m. to 6:30 p.m.</p><p>Televised Amended</p>
        <p>Artificial Intelligence in Health Care</p><p>Witnesses Amended</p><p>Canadian Medical Association</p>
        <ul><li>Dr. A B, President ( by videoconference )</li></ul><p>Clerk of the committee</p><p>Jane (613-555-1212)</p></body></html>"""
        subject, rest = P.agenda_of(P.notice_lines(page))
        self.assertEqual(subject, "Artificial Intelligence in Health Care")
        self.assertEqual(rest, ["Canadian Medical Association", "Dr. A B, President"])

    def test_bill_word_lists(self):
        import re

        def side(title):
            title = re.sub(r"(?i)\b(soil|animal|plant|forest|ocean) health\b|child care", " ", title)
            return next((a for a in ("BOTH", "CADA", "HEAL") if re.search(P.BILL_WORDS[a], title, re.I)), None)
        self.assertEqual(side("Used Car Tax Cut Act"), "CADA")
        self.assertEqual(side("Mental Health Parity Act"), "HEAL")
        self.assertEqual(side("Budget Implementation Act"), "BOTH")
        self.assertIsNone(side("Jury Duty Appreciation Week Act"))
        self.assertIsNone(side("National Strategy for Soil Health Act"))

    def test_bill_progress_steps(self):
        house = {"PassedHouseFirstReadingDateTime": "2026-10-01T00:00:00", "PassedHouseSecondReadingDateTime": "2026-10-20T00:00:00"}
        self.assertEqual(sum(1 for t in P.bill_steps(house) if t), 2)
        senate = dict(house, IsSenateBill=True, PassedSenateFirstReadingDateTime="2026-09-01T00:00:00")
        self.assertTrue(P.bill_steps(senate)[0] and P.bill_steps(senate)[3])

    def test_notes_are_cached_and_irrelevant_bills_dropped(self):
        P_get, P_sp = P.bill_summary, P.bill_sponsor
        P.bill_summary = lambda na, num: "This enactment does something."
        P.bill_sponsor = lambda na, num: "Jane Doe (MP for Somewhere)"
        try:
            data = {"bills": [{"num": "C-1", "title": "A", "long": "", "assoc": "HEAL", "sponsor": ""},
                              {"num": "C-2", "title": "B", "long": "", "assoc": "CADA", "sponsor": ""}],
                    "meetings": [{"url": "/m/1", "committee": "Health", "code": "HESA", "date": "2026-11-25", "subject": "AI",
                                  "agenda": ["CMA"], "assoc": "HEAL", "topic": "", "witness": ""}]}
            asked = []

            def ask(bills, meetings):
                asked.append((len(bills), len(meetings)))
                return {"bills": [{"n": 0, "note": "Does a thing.", "relevance": 2}, {"n": 1, "note": "x", "relevance": 0}],
                        "meetings": [{"n": 0, "priority": 2, "assoc": "HEAL", "why": "On file"}]}
            P.annotate(None, data, NOW, ask)
            self.assertEqual([b["num"] for b in data["bills"]], ["C-1"])
            self.assertEqual(data["bills"][0]["note"], "Does a thing.")
            self.assertEqual(data["meetings"][0]["priority"], 2)
            P.annotate(None, data, NOW + 60, ask)
            self.assertEqual(asked, [(2, 1), (0, 0)])        # nothing is asked twice
        finally:
            P.bill_summary, P.bill_sponsor = P_get, P_sp


class Health(unittest.TestCase):
    def local(self, hour):
        return datetime(2026, 11, 24, hour, 0, tzinfo=ZoneInfo("America/Toronto"))

    def test_claude_failing_alerts_once_then_reports_recovery(self):
        state, sent = {}, []
        alert = lambda title, msg, tags: sent.append(title)   # noqa: E731
        for n in range(5):
            N.health_check(state, NOW + n * 300, False, self.local(12), True, False, [], alert)
        self.assertEqual(len(sent), 1)
        N.health_check(state, NOW + 2000, False, self.local(12), True, True, [], alert)
        self.assertEqual(len(sent), 2)
        self.assertIn("again", sent[1])

    def test_no_alerts_overnight(self):
        state, sent = {}, []
        for n in range(5):
            N.health_check(state, NOW + n * 300, True, self.local(2), True, False, [], lambda *a: sent.append(a))
        self.assertEqual(sent, [])

    def test_source_down_a_day_is_reported_once_a_day(self):
        state, sent = {}, []
        alert = lambda title, msg, tags: sent.append(msg)   # noqa: E731
        N.health_check(state, NOW, False, self.local(12), False, False, ["feed:x"], alert)
        self.assertEqual(sent, [])
        for extra in (25 * 3600, 25 * 3600 + 300):
            N.health_check(state, NOW + extra, False, self.local(13), False, False, ["feed:x"], alert)
        self.assertEqual(len(sent), 1)
        self.assertIn("x", sent[0])

    def test_watchdog_decisions(self):
        self.assertEqual(health.decide(5, {}, NOW, 12)[0], [])
        self.assertEqual(health.decide(25, {}, NOW, 12)[0], ["restart"])
        actions, state = health.decide(50, {}, NOW, 12)
        self.assertEqual(actions, ["restart", "alert"])
        self.assertEqual(health.decide(60, state, NOW + 600, 12)[0], ["restart"])      # no repeat within six hours
        self.assertEqual(health.decide(50, {}, NOW, 3)[0], ["restart"])                # quiet overnight
        self.assertEqual(health.decide(3, state, NOW + 900, 12)[0], ["recovered"])


class CommitteeMail(unittest.TestCase):
    MAIL = """Dear Subscriber,
As requested, here are the new Committee items posted on the parliamentary website since your last update:
Health (HESA)
News Release:
Committee Begins Study of Artificial Intelligence in Health Care<https://www.ourcommons.ca/DocumentViewer/en/111>
Official Languages (LANG)
News Release:
Use of French in Private Businesses<https://www.ourcommons.ca/DocumentViewer/en/222>
Unsubscribe <https://www.ourcommons.ca/cmte-subscription/en/NewsletterUnsubscribe?UnsubscribeCode=x>
See also <https://evil.example/DocumentViewer/en/333>"""
    PAGE = """<html><head><meta name="description" content="Committee News Release - November 24, 2026 - HESA (45-1) - House of Commons of Canada" />
<meta name="Institution" content="Health" /></head><body><nav>Menu</nav><p>NEWS RELEASE</p><p>For immediate release</p>
<p>Committee Begins Study of Artificial Intelligence in Health Care</p>
<p>Ottawa, November 24, 2026</p>
<p>The Standing Committee on Health will study the use of artificial intelligence, including AI scribes, in health care across Canada.</p>
<p>For more information, please contact:</p><p>Clerk of the committee</p></body></html>"""

    def test_only_house_documents_are_taken_from_the_mail(self):
        found = C.parse_mail(self.MAIL)
        self.assertEqual([f["url"][-3:] for f in found], ["111", "222"])
        self.assertEqual((found[0]["code"], found[0]["kind"]), ("HESA", "News Release"))
        self.assertIn("Artificial Intelligence", found[0]["hint"])

    def test_document_page_is_read(self):
        doc = C.parse_doc(self.PAGE, "https://www.ourcommons.ca/DocumentViewer/en/111")
        self.assertEqual((doc["code"], doc["committee"], doc["kind"]), ("HESA", "Health", "News Release"))
        self.assertEqual(datetime.fromtimestamp(doc["ts"], ZoneInfo("America/Toronto")).strftime("%Y-%m-%d"), "2026-11-24")
        self.assertIn("AI scribes", doc["text"])
        self.assertNotIn("Clerk", doc["text"])
        self.assertEqual(doc["title"], "Committee Begins Study of Artificial Intelligence in Health Care")

    def test_items_are_collected_once_and_filed(self):
        state, sent = {}, []
        inbox = [{"id": "m1", "time": 0, "title": "Committee Subscription", "text": self.MAIL}]
        items = C.collect(state, NOW, N, lambda *a: sent.append(a), inbox=inbox, fetch=lambda url: self.PAGE)
        self.assertEqual(len(items), 2)
        self.assertEqual(N.classify(items[0])["assoc"], "HEAL")
        self.assertEqual(C.collect(state, NOW + 300, N, lambda *a: sent.append(a), inbox=inbox, fetch=lambda url: self.PAGE), [])
        path = os.path.join(tempfile.mkdtemp(), "news.json")
        C.record(path, items, NOW, N)
        import json
        rel = json.load(open(os.path.join(os.path.dirname(path), "parliament.json")))["releases"]
        self.assertEqual(len(rel), 2)
        self.assertEqual(sent, [])

    def test_long_mail_is_worked_through_over_several_runs_and_notices_skipped(self):
        lines = ["Health (HESA)", "Notice of Meeting:", "Meeting 47<https://www.ourcommons.ca/DocumentViewer/en/900>", "News Release:"]
        lines += [f"Release {n}<https://www.ourcommons.ca/DocumentViewer/en/{1000 + n}>" for n in range(30)]
        inbox = [{"id": "big", "time": 0, "title": "Committee Subscription", "text": "\n".join(lines)}]
        state, got = {}, []
        for run in range(4):
            got += C.collect(state, NOW + run * 300, N, lambda *a: None, inbox=inbox, fetch=lambda url: self.PAGE)
        links = [i["link"] for i in got]
        self.assertEqual(len(links), 30)                      # every release, none twice
        self.assertEqual(len(set(links)), 30)
        self.assertFalse(any(link.endswith("/900") for link in links))   # the meeting notice is left out

    def test_confirmation_request_is_passed_on_once(self):
        state, sent = {}, []
        inbox = [{"id": "c1", "time": 0, "title": "Please confirm your subscription",
                  "text": "Confirm: https://www.ourcommons.ca/cmte-subscription/en/Confirm?code=abc"}]
        for _ in range(2):
            C.collect(state, NOW, N, lambda *a: sent.append(a), inbox=inbox, fetch=lambda url: "")
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][2].startswith("https://www.ourcommons.ca/"))


class GoLinks(unittest.TestCase):
    def test_lookup_file_holds_recent_alerts_only(self):
        import json
        path = os.path.join(tempfile.mkdtemp(), "news.json")
        a = dict(item("Old alert"), link="https://example.com/old")
        b = dict(item("New alert"), link="https://example.com/new")
        N.save_go_links(path, [a], NOW - 4 * 86400)
        N.save_go_links(path, [b, dict(item("Bad"), link="javascript:alert(1)")], NOW)
        links = json.load(open(os.path.join(os.path.dirname(path), "go.json")))
        self.assertEqual(list(links), [N.item_id(b)])
        self.assertEqual(links[N.item_id(b)][0], "https://example.com/new")


class NewSources(unittest.TestCase):
    def fr(self, title):
        t = N.classify(dict(item(title), lang="fr"))
        return t["key"] if t else None

    def test_french_headlines_are_filed(self):
        self.assertEqual(self.fr("Tarifs américains : Ottawa veut protéger le secteur automobile"), "trade")
        self.assertEqual(self.fr("Stellantis : l'usine de Brampton pourrait être vendue"), "plants")
        self.assertEqual(self.fr("Ottawa bonifie les transferts en santé aux provinces"), "healthfunding")
        self.assertEqual(self.fr("L'intelligence artificielle pour aider les médecins de famille"), "aihealth")
        self.assertIsNone(self.fr("Le Canadien l'emporte 3-2 en prolongation"))

    def test_federal_register_json_is_read(self):
        import json
        data = json.dumps({"results": [
            {"title": "Adjusting Imports of Automobiles and Automobile Parts Into the United States", "abstract": "Proclamation",
             "html_url": "https://www.federalregister.gov/d/1", "publication_date": "2026-11-23", "document_number": "2026-1"},
            {"title": "No link here"}]}).encode()
        got = list(N.parse_fedreg(data, "U.S. Federal Register"))
        self.assertEqual(len(got), 1)
        self.assertEqual(N.classify(dict(got[0], context="Canada U.S. trade tariff"))["key"], "trade")
        self.assertLess(abs(got[0]["ts"] - NOW), 3 * 86400)

    def test_gazette_issue_is_opened_into_entries(self):
        page = ('<a href="/rp-pr/p2/2026/2026-11-18/html/sor-dors250-eng.html">Regulations Amending the Electric Vehicle '
                'Availability Standard</a><a href="index-eng.html">HTML</a><a href="/rp-pr/p2/2026/2026-11-18/pdf/g2.pdf">PDF</a>'
                '<a href="/rp-pr/p2/2026/2026-11-18/html/si-tr90-eng.html">Certain Motor Vehicles Remission Order</a>')
        got = N.gazette_entries(page, "https://www.gazette.gc.ca/rp-pr/p2/2026/2026-11-18/html/index-eng.html")
        self.assertEqual([t for t, _ in got], ["Regulations Amending the Electric Vehicle Availability Standard",
                                               "Certain Motor Vehicles Remission Order"])
        self.assertTrue(all(link.startswith("https://www.gazette.gc.ca/rp-pr/") for _, link in got))

    def test_gazette_part_one_notices_are_read(self):
        page = ('<h2>Menu</h2><h2 id="ne1">DEPARTMENT OF FINANCE</h2><h3>CUSTOMS TARIFF</h3><h4>Motor Vehicles Remission Order</h4>'
                '<h2 id="ne2">DEPARTMENT OF HEALTH</h2><h3>FOOD AND DRUGS ACT</h3><h4>Notice of fees</h4><h4>Part 2</h4>')
        got = N.gazette_notices(page, "https://gazette.gc.ca/x/notice-avis-eng.html")
        self.assertEqual(got, [("Motor Vehicles Remission Order (Department Of Finance)", "https://gazette.gc.ca/x/notice-avis-eng.html#ne1"),
                               ("Notice of fees (Department Of Health)", "https://gazette.gc.ca/x/notice-avis-eng.html#ne2")])

    def test_feed_titles_with_nested_markup_are_read(self):
        atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">New motor
        vehicle sales, <span>September 2026</span></div></title><link href="https://www.statcan.gc.ca/x"></link>
        <updated>2026-11-24T08:30:00-05:00</updated></entry></feed>"""
        got = list(N.parse(atom, "Statistics Canada"))
        self.assertEqual(got[0]["title"], "New motor vehicle sales, September 2026")

    def test_every_source_has_a_unique_name_and_a_secure_address(self):
        srcs = list(N.all_sources())
        self.assertEqual(len({s_["id"] for s_ in srcs}), len(srcs))
        self.assertTrue(all(s_["url"].startswith("https://") for s_ in srcs))


class Icons(unittest.TestCase):
    def test_image_checks(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", 180, 180) + b"x" * 200
        tiny = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", 16, 16) + b"x" * 200
        self.assertEqual(N.image_ext(png), "png")
        self.assertEqual(N.image_ext(tiny), "")
        self.assertEqual(N.image_ext(b"<html>not an image</html>" * 20), "")

    def test_outlet_sites(self):
        self.assertEqual(N.outlet_site({"source": "The Globe and Mail"}), "theglobeandmail.com")
        self.assertEqual(N.outlet_site({"source": "The Hub | More Signal."}), "thehub.ca")
        self.assertEqual(N.outlet_site({"source": "Unknown Outlet"}), "")


class Page(unittest.TestCase):
    def test_page_has_its_working_parts(self):
        html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
        for needed in ('id="list"', 'id="tabs"', 'id="filters"', 'id="ptr"', "function render()", "function renderParl(",
                       "function checkForUpdate("):
            self.assertIn(needed, html, needed)
        go = open(os.path.join(ROOT, "go.html"), encoding="utf-8").read()
        self.assertLess(len(go), 6000)                      # the point of this page is that it is small
        self.assertIn("location.replace", go)


if __name__ == "__main__":
    unittest.main()
