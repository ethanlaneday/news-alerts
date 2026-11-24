#!/usr/bin/env python3
"""Phone alerts for news that matters to CADA (Canadian Automobile Dealers
Association): CADA itself, trade/CUSMA, auto plants, EV policy, and the market.

Sources: Google News and Bing News searches, plus direct feeds from Canadian
outlets, auto trade press and government newsrooms. Every headline is given an
importance level (1-5) that is shown on the notification and sets how loudly
your phone alerts. Levels come from built-in rules, or from Claude if an
ANTHROPIC_API_KEY is configured (rules are the fallback).

Pushes go to the ntfy app (https://ntfy.sh, free). Run by
.github/workflows/news-alerts.yml every 5 minutes.

Environment:
  NTFY_TOPIC         required  the private topic you subscribe to in the ntfy app
  NTFY_SERVER        optional  defaults to https://ntfy.sh
  ANTHROPIC_API_KEY  optional  turns on Claude importance scoring
  CLAUDE_MODEL       optional  defaults to claude-opus-5-5
  MIN_LEVEL          optional  don't notify below this level (default 2)
  STATE_FILE         optional  default .cada-state/seen.json
  CHECK_FEEDS=1      optional  test every source, send a health report, change nothing
  DRY_RUN=1          optional  print instead of sending
  FEED_FILE          optional  read this RSS/Atom file for every source (testing)
"""
from __future__ import annotations

import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# TOPICS: what counts as relevant. Edit freely.
#   require: every regex must match the headline+summary (keeps it on-topic and Canadian)
#   queries: searches run on Google News and Bing News
#   base:    starting importance level for this topic (1-5)
# ---------------------------------------------------------------------------
CANADA = (r"canad|ontario|quebec|québec|ottawa|carney|leblanc|champagne|joly|doug ford|"
          r"windsor|oshawa|brampton|oakville|alliston|woodstock|cambridge|ingersoll|st\. thomas|"
          r"toronto|montreal|vancouver|alberta|b\.c\.|british columbia|cusma|unifor|"
          r"nova scotia|new brunswick|manitoba|saskatchewan|newfoundland|labrador|prince edward island|\bp\.e\.i\.|"
          r"yukon|nunavut|northwest territories|atlantic canada|maritimes|calgary|edmonton|winnipeg|regina|"
          r"saskatoon|halifax|fredericton|moncton|charlottetown|st\. john's|mississauga|markham|kitchener|"
          r"(?<=, )(ont|que|alta|sask|man|n\.s|n\.b|nfld|b\.c)\.")   # dateline style: "Winnipeg, Man."
AUTO = r"\bauto|automo|vehicle|\bcars?\b|\bev\b|\bevs\b|electric vehicle|dealer|dealership|" \
       r"stellantis|chrysler|jeep|\bgm\b|general motors|ford motor|ford's|honda|toyota|hyundai|" \
       r"volkswagen|\bvw\b|tesla|nissan|kia|byd|battery|steel|aluminum|\bford\b(?! government)|unifor|automaker"
# Words that really mean the car industry. Unifor, steel and aluminum also cover
# forestry, media, mills etc., so plant and market stories need one of these.
AUTO_CORE = r"\bauto|automo|vehicle|\bcars?\b|\bev\b|\bevs\b|electric vehicle|dealer|dealership|" \
            r"stellantis|chrysler|jeep|\bgm\b|general motors|ford motor|ford's|honda|toyota|hyundai|" \
            r"volkswagen|\bvw\b|tesla|nissan|kia|byd|ev battery|battery plant|battery maker|" \
            r"\bford\b(?! government)|automaker|assembly plant|\bcami\b"

# Associations: every topic belongs to one, and every alert starts with its name
# and emoji so you can tell at a glance who the news is for.
ASSOCS = {
    "CADA": {"emoji": "red_car", "name_re": r"(?i:canadian automobile dealers)|\bCADA\b"},
    "HEAL": {"emoji": "hospital", "name_re": r"(?i:organizations for health action|health action lobby)|\bHEAL\b"},
}

CANADA_H = CANADA + r"|provinc|\bfpt\b|health human resources|\bcihi\b|health workforce canada|camimh"   # health news often says "provinces", not "Canada"
AI_WORDS = re.compile(
    r"(?i:artificial intelligence|\bai[- ]powered|\bai[- ]scribes?|medical scribes?|\bscribes?\b|ambient|"
    r"dictation|clinical documentation|machine learning|large language models?|\bllms?\b|chatbots?|generative)|"
    r"\bAI\b")
HEALTH = r"health|hospital|nurs|physician|doctor|pharma|drug|dental|mental health|long-term care|" \
         r"care home|patient|medic|wait time|primary care|paramedic|psycholog|therap|chiropract"

TOPICS = [
    {"key": "cada", "assoc": "CADA", "label": "CADA", "base": 3,
     "require": [r"canadian automobile dealers|\bCADA\b|cada\.ca"],
     "queries": ['"Canadian Automobile Dealers Association"',
                 'CADA (dealer OR dealers OR dealership OR automotive OR auto) Canada -Colorado -California',
                 "site:cada.ca"]},
    {"key": "trade", "assoc": "CADA", "label": "Trade/CUSMA", "base": 3,
     "require": [CANADA + r"|usmca|mexico", r"cusma|usmca|tariff|surtax|trade|duty|duties|countermeasure|remission|quota|import"],
     "queries": ["CUSMA OR USMCA",
                 '"CUSMA review" OR "USMCA review" OR "joint review"',
                 "Canada (tariff OR tariffs) (auto OR autos OR automotive OR vehicles OR cars)",
                 "Canada U.S. trade talks (auto OR autos OR steel OR LeBlanc)",
                 '"section 232" Canada (auto OR steel OR aluminum)',
                 "Canada China EV tariff",
                 "(Carney OR LeBlanc OR Joly OR Champagne) (auto OR autos OR automotive OR tariffs OR CUSMA)",
                 # The industry voices reporters quote first on tariffs and plants
                 '"Flavio Volpe" OR APMA OR "Canadian Vehicle Manufacturers" OR "Global Automakers of Canada"']},
    {"key": "plants", "assoc": "CADA", "label": "Plants", "base": 3,
     "require": [CANADA, AUTO_CORE,
                 r"plant|factory|assembly|production|retool|shift|layoff|laid off|jobs|workers|invest|"
                 r"battery|gigafactory|clos|shut|moving|relocat|sold|sale of|\bbuy(s|ing)?\b|buyout|acquir|strike|unifor|idle"],
     "queries": ["Canada (auto OR assembly OR EV OR battery) plant",
                 "Ontario auto plant",
                 # One search per plant: Google News can't parse "(A B) OR (C D)" chains.
                 "Stellantis Brampton", "Stellantis Windsor plant", "CAMI Ingersoll", "GM Oshawa plant",
                 "Ford Oakville plant", "Honda Alliston", "Toyota (Woodstock OR Cambridge) plant",
                 "Volkswagen St. Thomas battery plant",
                 "Canada automaker (production OR layoffs OR investment OR moving OR relocate)",
                 "Unifor (GM OR Ford OR Stellantis OR plant)"]},
    {"key": "evpolicy", "assoc": "CADA", "label": "EV policy", "base": 3,
     "require": [CANADA, r"\bev\b|\bevs\b|electric|zev|izev|zero-emission|zero emission|mandate|rebate"],
     "queries": ['"EV Availability Standard" OR "EV mandate" Canada',
                 "iZEV OR (EV rebate Canada) OR (EV incentive Canada)",
                 "Canada electric vehicle (policy OR mandate OR rebate OR sales target)",
                 "Canada Chinese electric vehicles"]},
    {"key": "market", "assoc": "CADA", "label": "Market", "base": 2,
     "require": [CANADA, AUTO_CORE],
     "queries": ["Canada (new vehicle OR auto OR car) sales",
                 "DesRosiers",
                 "Canada (car dealers OR car dealership OR auto dealers)",
                 "Canada (auto loan OR car prices OR vehicle affordability)",
                 "Canada (vehicle recall OR car theft) Transport Canada OR police",
                 "OMVIC OR AMVIC OR (dealer franchise law Canada)"]},
    {"key": "economy", "assoc": "CADA", "label": "Rates/economy", "base": 2,
     "require": [CANADA + r"|bank of canada",
                 r"policy rate|interest rates?|rate (decision|cut|hike|announcement)|"
                 r"(cuts?|holds?|raises?|maintains?) (its |key |policy |interest |overnight )*rate|"
                 r"new motor vehicle sales"],
     "queries": ['"Bank of Canada" (rate OR rates) decision']},
    # ---- HEAL: Organizations for Health Action ----
    # HEAL follows federal health policy broadly. Terms from its January 2025 consensus document
    # (health human resources, mental health parity, community-based care) widen these topics as an
    # extra net; they don't make those areas more important than the rest.
    {"key": "heal", "assoc": "HEAL", "label": "HEAL", "base": 3,
     "require": [re.compile(r"(?i:organizations for health action|health action lobby)|\bHEAL\b")],
     "queries": ['"Organizations for Health Action"',
                 '"HEAL" coalition health Canada',
                 "site:healthaction.ca"]},  # HEAL's site
    # MP Michael Ma: his AI and health initiatives, including legislation he's expected to lead.
    {"key": "michaelma", "assoc": "HEAL", "label": "Michael Ma", "base": 3,
     "require": [re.compile(r"\bMichael Ma\b"),
                 re.compile(r"(?i:bill|legislation|motion|petition|artificial intelligence|health|committee|tabled|"
                            r"introduc|private member|scribe|technology|digital)|\bAI\b")],
     "queries": ['"Michael Ma" MP',
                 '"Michael Ma" (AI OR "artificial intelligence" OR bill OR health)']},
    # AI in health care: AI scribes / ambient dictation, clinical AI, and rules for AI in health.
    {"key": "aihealth", "assoc": "HEAL", "label": "AI in health", "base": 3,
     "require": [CANADA_H, AI_WORDS, HEALTH + r"|clinic|legislation|\bbill\b|regulat"],
     "queries": ['"AI scribe" OR "AI scribes" Canada',
                 'Canada doctors AI (scribe OR scribes OR documentation OR paperwork)',
                 'Canada "artificial intelligence" health care (doctors OR nurses OR hospitals OR patients)',
                 'Canada AI health (legislation OR regulation OR bill OR privacy)']},
    {"key": "mentalhealth", "assoc": "HEAL", "label": "Mental health", "base": 3,
     "require": [CANADA_H, r"mental health|mental illness|addiction|substance use|opioids?|overdose|suicide|psycholog|"
                           r"psychiatr|\bmhsuh\b|camimh|parity"],
     "queries": ['("mental health transfer" OR "mental health parity" OR CAMIMH) Canada',
                 '"mental health" Canada federal (funding OR strategy OR legislation)',
                 "Canada (opioid OR \"substance use\") federal"]},
    {"key": "healthfunding", "assoc": "HEAL", "label": "Health funding", "base": 3,
     "require": [CANADA_H, HEALTH,
                 r"health transfer|health funding|funding for health|health care funding|health accord|"
                 r"bilateral|budget|billion|fiscal|transfer payment|canada health act|user fees?|extra.billing|"
                 r"private clinics?|economic statement|pre-budget"],
     "queries": ['"Canada Health Transfer" OR "health transfer"',
                 "federal health funding provinces Canada",
                 "federal budget health care Canada",
                 '"Canada Health Act" (private OR clinics OR deductions OR "user fees")',
                 '("pre-budget consultations" OR "fall economic statement") health']},
    {"key": "pharmacare", "assoc": "HEAL", "label": "Pharmacare/drugs", "base": 3,
     "require": [CANADA_H + r"|pharmacare", r"pharmacare|dental care|dental plan|drug coverage|prescription drug|formulary|"
                         r"drug plan|medication coverage|drug shortages?|shortage of .{0,30}(drug|medication)|"
                         r"canada's drug agency|cda-amc|pmprb|drug pric|rare disease|biosimilar"],
     "queries": ["pharmacare Canada",
                 '"Canadian Dental Care Plan" OR "national dental care"',
                 'Canada ("drug shortage" OR "drug shortages")',
                 '"Canada\'s Drug Agency" OR PMPRB OR "rare disease strategy"']},
    {"key": "workforce", "assoc": "HEAL", "label": "Health workforce", "base": 3,
     "require": [CANADA_H,
                 r"shortage|workforce|human resources|licensure|licensing|recruit|retention|burnout|staffing|"
                 r"credential|internationally (educated|trained)|scope of practice|labour mobility|labor mobility|"
                 r"mutual recognition|interprovincial mobility|accreditation|bridging program|health human resources?|"
                 r"workforce (plan|strategy|data)|health workforce canada|\bcihi\b",
                 r"nurs|physician|doctor|health|paramedic|pharmacist|psycholog|therapist|chiropract|dentist"],
     "queries": ["Canada health workforce shortage",
                 '"national licensure" OR "Health Workforce Canada" OR "health human resources"',
                 "Canada (nurses OR doctors OR physicians) shortage federal",
                 "Canada health professionals (\"labour mobility\" OR \"credential recognition\")",
                 '("health human resources" OR "health workforce") Canada',
                 '"internationally trained" (nurses OR doctors OR physicians) Canada',
                 'Canada health care workers burnout federal']},
    {"key": "healthdata", "assoc": "HEAL", "label": "Health data", "base": 3,
     "require": [CANADA_H, r"health data|health information|interoperab|electronic (health|medical) records?|"
                         r"digital health|virtual care|connected care|health records"],
     "queries": ['"Connected Care" OR "health data" Canada federal',
                 "Canada health records interoperability"]},
    {"key": "community", "assoc": "HEAL", "label": "Community-based care", "base": 2,
     "require": [CANADA_H, r"community[- ]based care|community care|community health cent(re|er)s?|team-based care|"
                           r"interprofessional|inter-professional|collaborative care|community paramedic|"
                           r"mobile (health )?clinics?|school-based health|home and community care|"
                           r"telehealth|telemedicine|remote (patient )?monitoring|children's health|"
                           r"pa?ediatric|child(ren)?'s mental health|school food|school nutrition"],
     "queries": ['"community health centres" OR "team-based care" OR "community-based care" Canada',
                 '"home and community care" OR "community paramedicine" Canada',
                 'Canada (pediatric OR "children\'s health") federal',
                 'Canada ("virtual care" OR telehealth)']},
    {"key": "access", "assoc": "HEAL", "label": "Access to care", "base": 2,
     "require": [CANADA_H, r"family (doctor|physician)s?|primary care|team-based care|wait (times?|lists?)|"
                         r"emergency (room|department)s?|\ber\b closures?|access to care|unattached patients|walk-in"],
     "queries": ["Canada primary care family doctors federal",
                 'Canada health "wait times"']},
    {"key": "aging", "assoc": "HEAL", "label": "Aging/caregiving", "base": 2,
     "require": [CANADA_H, r"long-term care|home care|seniors|aging|ageing|dementia|caregivers?|palliative",
                 HEALTH + r"|care"],
     "queries": ['"long-term care" OR "home care" Canada federal',
                 "Canada dementia OR caregivers national strategy",
                 '"palliative care" Canada federal',
                 '"Canadian Centre for Caregiving Excellence" OR "unpaid caregivers" Canada']},
    {"key": "publichealth", "assoc": "HEAL", "label": "Public health", "base": 2,
     "require": [CANADA_H, r"public health agency|\bphac\b|vaccin|outbreak|measles|pandemic|"
                         r"chief public health officer|immuni[sz]"],
     "queries": ['"Public Health Agency of Canada" OR "chief public health officer"',
                 "Canada (measles OR outbreak OR vaccine) federal"]},
    {"key": "indigenoushealth", "assoc": "HEAL", "label": "Indigenous health", "base": 2,
     "require": [CANADA_H, r"indigenous|first nations|inuit|métis|metis|jordan's principle", HEALTH],
     "queries": ['"Indigenous health" Canada federal']},
    {"key": "healthpolicy", "assoc": "HEAL", "label": "Health policy", "base": 2,
     "require": [CANADA_H, HEALTH,
                 r"health canada|health minister|minister of health|federal|parliament|commons|senate|"
                 r"committee|bill c-|legislation|national strategy|standards|health ministers|fpt"],
     "queries": ['"health minister" Canada federal',
                 '"Health Canada" announces',
                 "Canada mental health federal funding",
                 "long-term care national standards Canada",
                 '"health committee" (MPs OR Commons OR Parliament) Canada',
                 "site:healthydebate.ca",  # its feed blocks readers, so search it instead
                 "Senate social affairs committee health",
                 "federal provincial territorial health ministers meeting",
                 # Statements from HEAL members and allied national health organizations
                 '("Canadian Medical Association" OR "Canadian Nurses Association" OR "Canadian Pharmacists Association") federal',
                 '("Canadian Psychological Association" OR "Canadian Physiotherapy Association" OR '
                 '"Canadian Association of Social Workers" OR "Canadian Chiropractic Association")',
                 '(HealthCareCAN OR "Alzheimer Society of Canada" OR "Obesity Canada" OR '
                 '"Canadian Dental Association") federal',
                 '("Pallium Canada" OR "Canadian Association of Community Health Centres" OR '
                 '"Children\'s Healthcare Canada" OR "Canadian Psychiatric Association")',
                 '("Canadian Society of Respiratory Therapists" OR "Dietitians of Canada" OR '
                 '"Speech-Language and Audiology Canada" OR "Canadian Association of Midwives")',
                 '("Canadian Dental Hygienists Association" OR "Canadian Association of Optometrists" OR '
                 '"Canadian Counselling and Psychotherapy Association" OR "Canadian College of Health Leaders")',
                 '("Association of Faculties of Medicine of Canada" OR "Royal College of Physicians and Surgeons" OR '
                 '"Canadian Society for Medical Laboratory Science" OR "Canadian Association of Medical Radiation '
                 'Technologists") federal']},
]

# Direct feeds (not searches). Financial Post and Windsor Star block feed readers
# (as does The Hill Times) and the White House has no feed; Google/Bing searches still cover them. Everything is filtered through TOPICS above, so a
# general business feed only produces alerts for relevant stories.
# "official": government sources get a bump in importance.
FEEDS = [
    {"id": "cbc-business", "title_only": True, "name": "CBC News", "url": "https://www.cbc.ca/webfeed/rss/rss-business"},
    {"id": "cbc-windsor", "title_only": True, "name": "CBC Windsor", "url": "https://www.cbc.ca/webfeed/rss/rss-canada-windsor"},
    {"id": "globe-business", "title_only": True, "name": "The Globe and Mail",
     "url": "https://www.theglobeandmail.com/arc/outboundfeeds/rss/category/business/"},
    {"id": "global-money", "title_only": True, "name": "Global News", "url": "https://globalnews.ca/money/feed/"},
    {"id": "driving-ca", "name": "Driving.ca", "url": "https://driving.ca/feed/"},
    {"id": "cad-magazine", "name": "Canadian Auto Dealer", "url": "https://canadianautodealer.ca/feed/"},
    {"id": "electric-autonomy", "name": "Electric Autonomy Canada", "url": "https://electricautonomy.ca/feed/"},
    # Health (HEAL)
    {"id": "cbc-health", "name": "CBC News", "title_only": True, "context": "Canada",
     "url": "https://www.cbc.ca/webfeed/rss/rss-health"},
    {"id": "global-health", "name": "Global News", "title_only": True, "url": "https://globalnews.ca/health/feed/"},
    {"id": "hospital-news", "name": "Hospital News", "context": "Canada", "url": "https://hospitalnews.com/feed/"},
    {"id": "conversation-health", "name": "The Conversation Canada", "title_only": True,
     "url": "https://theconversation.com/ca/health/articles.atom"},
    {"id": "policy-options", "name": "Policy Options", "title_only": True, "url": "https://policyoptions.irpp.org/feed/"},
    # Ottawa / Parliament Hill
    {"id": "cbc-politics", "title_only": True, "name": "CBC News", "url": "https://www.cbc.ca/webfeed/rss/rss-politics"},
    {"id": "globe-politics", "title_only": True, "name": "The Globe and Mail",
     "url": "https://www.theglobeandmail.com/arc/outboundfeeds/rss/category/politics/"},
    {"id": "ipolitics", "title_only": True, "name": "iPolitics", "url": "https://www.ipolitics.ca/feed/"},
    # This feed carries lots of U.S. stories, so judge it on the headline alone.
    {"id": "an-canada", "name": "Automotive News Canada", "title_only": True,
     "url": "https://www.autonews.com/arc/outboundfeeds/rss/category/canada/"},
    # What's said in the House and committees (Hansard via openparliament.ca, usually a day behind).
    # "parl" is the phrase searched; "topic" files the mention under that topic directly.
    *[{"id": "parl-" + pid, "name": "Parliament (Hansard)", "official": True, "parl": phrase, "topic": tkey,
       "max_age_hours": 120,
       "url": "https://openparliament.ca/search/feed/?q=" + urllib.parse.quote_plus(q)}
      for pid, phrase, tkey, q in [
          ("auto-dealers", "automobile dealers", "cada", '"automobile dealers"'),
          ("car-dealers", "auto dealers", "cada", '"auto dealers"'),
          ("ev-standard", "the EV Availability Standard", "evpolicy", '"Electric Vehicle Availability Standard"'),
          ("heal", "Organizations for Health Action", "heal", '"Organizations for Health Action"'),
          ("hhr", "health human resources", "workforce", '"health human resources"'),
          ("mh-transfer", "the mental health transfer", "mentalhealth", '"mental health transfer"'),
          ("ai-scribe", "AI scribes", "aihealth", '"AI scribe"'),
          ("ai-health", "AI in health care", "aihealth", '"artificial intelligence" "health care"'),
      ]],
    # Official sources: often the first place news appears. (The Canada Gazette's feeds
    # only list whole issues, not individual regulations, and Statistics Canada's Daily
    # feed returns no entries to readers, so neither is used; news coverage of them is.) "context" marks them as
    # Canadian/federal for the topic filter, since their titles rarely say so.
    {"id": "legisinfo", "name": "Parliament of Canada (LEGISinfo)", "official": True, "title_only": True,
     "track_changes": True,  # same bill, new stage (e.g. third reading) = new alert
     "context": "Canada federal Parliament bill", "url": "https://www.parl.ca/legisinfo/en/bills/rss"},
    {"id": "bank-of-canada", "name": "Bank of Canada", "official": True, "title_only": True,
     "context": "Canada", "url": "https://www.bankofcanada.ca/content_type/press-releases/feed/"},
    # (CADA and HEAL have no working news feeds; their sites are searched via Google instead.)
    {"id": "gc-news", "title_only": True, "name": "Government of Canada", "official": True,
     "context": "Canada federal",
     "url": "https://api.io.canada.ca/io-server/gc/news/en/v2?sort=publishedDate&orderBy=desc&pick=100&format=atom"},
]
# Ottawa outlets searched via Google News (works even when an outlet has no
# usable feed, e.g. POLITICO's Ottawa coverage). Results still go through TOPICS.
OTTAWA_QUERIES = [
    "site:hilltimes.com (auto OR automotive OR tariff OR tariffs OR CUSMA OR EV OR dealers OR trade)",
    "site:politico.com (Canada OR Ottawa OR Carney) (CUSMA OR USMCA OR tariff OR tariffs OR auto OR autos OR EV)",
    'site:politico.com "Ottawa Playbook"',
    "site:ipolitics.ca (auto OR automotive OR tariff OR tariffs OR CUSMA OR EV OR trade)",
    "site:nationalnewswatch.com (auto OR tariff OR CUSMA OR EV)",
    "site:cpac.ca (auto OR tariff OR CUSMA OR EV)",
]
BING_QUERIES_PER_TOPIC = 2   # Bing News mirrors the first N searches of each topic

# ---------------------------------------------------------------------------
# IMPORTANCE RULES (used when Claude isn't configured, or as its fallback)
# ---------------------------------------------------------------------------
MAJOR = re.compile(  # +2: decisions and events that change things for dealers now
    r"deal (is )?(reached|struck|done)|agreement (reached|signed)|reach(es|ed)? (a )?deal|signs? deal|"
    r"impos(e|es|ed|ing)|slaps?|tariffs? (take|took|go|went) (into )?effect|lifts? tariffs?|removes? tariffs?|"
    r"scraps?|repeal|terminat|withdraw|walks? away|talks collapse|executive order|"
    r"to close|will close|closing|closure|shut(s|ting)? down|ends? production|cancel|"
    r"budget (tabled|released|unveiled|delivered)|royal assent|passes (third reading|the senate)|"
    r"funding (deal|agreement) (signed|reached)|signs? .{0,50}(agreement|deal)|health accord|"
    r"strike|walkout|lockout|\bmov(e|es|ing)\b[\w ]{0,25}production|production (to|out of)|relocat|leaving canada|exit(s|ing)? canada", re.I)
MINOR = re.compile(  # +1: notable developments
    r"talks|negotiat|deadline|review|retaliat|countermeasure|exempt|remission|quota|"
    r"layoff|lay off|laid off|job cuts|shift|idl(e|ed|ing)|invest|billion|new plant|retool|"
    r"rebate|incentive|mandate|pause|delay|recall|funding|budget|bill c-|pre-budget|consultation|"
    r"\btable[sd]?\b|introduc(es|ed|ing) (a |new )?(bill|legislation)|private member|"
    r"rate cut|cuts? (its |key |policy |interest )*rates?|holds? (its |key |policy |interest )*rates?|"
    r"rais(es|ed) (its |key |policy |interest )*rates?|"
    r"(maintains?|holds?) .{0,20}rate|second reading|third reading|report stage|referral to committee|"
    r"shortage|wait times?|licensure|pharmacare|dental care|outbreak|national strategy|private clinics?|cyber|outage|sold|sell|acquir|record|plunge|surge|warn", re.I)
SOFT = re.compile(   # -1: opinion and features rarely need a buzz
    r"^opinion|opinion:|column|analysis:|explainer|what to know|what you need|how to|podcast|"
    r"^watch|video:|quiz|\bbest\b|road test|first drive|^review:|\?$|"
    r"what we know|explained|a look back|looking back|recap|timeline|anniversary|one year (after|later|since)|"
    r"year in review|months? (after|later|since)|years? (after|later|since)|revisit|remember when", re.I)

LEVELS = {5: ("CRITICAL", "rotating_light"), 4: ("HIGH", "red_circle"),
          3: ("NOTABLE", "large_orange_diamond"), 2: ("FYI", "white_circle"), 1: ("LOW", "white_circle")}

# Outlets known to be reliable. Stories from other outlets lose one level and are
# only sent if they still reach NOTABLE (3), which keeps out aggregator/spam sites.
TRUSTED_SOURCES = re.compile(
    r"\bcbc\b|radio-canada|globe and mail|reuters|canadian press|\bctv|cp24|global news|toronto star|"
    r"financial post|national post|ottawa citizen|windsor star|montreal gazette|vancouver sun|calgary herald|"
    r"edmonton journal|bnn|bloomberg|associated press|^ap$|ap news|wall street journal|new york times|"
    r"washington post|financial times|\bbbc\b|\bcnbc\b|\bcnn\b|politico|hill times|ipolitics|"
    r"national newswatch|cpac|the logic|maclean|la presse|le devoir|automotive news|autonews|driving\.ca|"
    r"canadian auto dealer|electric autonomy|wards|detroit free press|detroit news|blackburn|am800|cklw|"
    r"windsorite|government of canada|canada\.ca|unifor|\bcada\b|canadian automobile dealers|"
    r"business in vancouver|toronto sun|winnipeg free press|halifax|chronicle herald|times colonist|"
    r"the canadian|canadian business|investing\.com|marketwatch|axios|the hill$|white house|"
    r"healthy debate|policy options|cmaj|hospital news|the conversation|canadian healthcare network|hospital news|medical post|"
    r"canadian medical association|nursing|healthing|health canada|cihi|statistics canada", re.I)

# Outlet tiers. TOP: wire services, national outlets, the trade press of record and official
# sources; preferred when several outlets carry the same story. LOW: partisan/opinion sites and
# enthusiast or aggregator blogs; their stories never alert and show on the page as FYI at most.
TOP_SOURCES = re.compile(
    r"reuters|canadian press|associated press|^ap$|ap news|bloomberg|\bcbc\b|radio-canada|globe and mail|\bctv|"
    r"global news|toronto star|financial post|national post|automotive news|autonews|politico|hill times|"
    r"ipolitics|la presse|le devoir|wall street journal|new york times|financial times|government of canada|"
    r"parliament|house of commons|senate of canada|bank of canada|health canada", re.I)
LOW_SOURCES = re.compile(
    r"rebel news|true north|western standard|juno news|post millennial|epoch times|blacklock|"
    r"ford authority|gm authority|mopar insiders|carscoops|autoevolution|carbuzz|hotcars|motorbiscuit|"
    r"torque news|the truth about cars|blogto|narcity|daily hive|yahoo|\baol\b|substack|medium\.com", re.I)


def outlet_tier(it) -> int:
    """3 = top, 2 = known/trusted, 1 = unfamiliar, 0 = low."""
    src = it.get("source") or ""
    if it.get("official") or TOP_SOURCES.search(src):
        return 3
    if LOW_SOURCES.search(src):
        return 0
    return 2 if TRUSTED_SOURCES.search(src) else 1


def apply_outlet_rules(it, after_ai=False):
    """Unfamiliar outlets lose a level; low-tier outlets are capped at FYI and never alert."""
    tier = outlet_tier(it)
    if tier == 0:
        it["lowtier"] = it["no_alert"] = True
        it["level"] = 2 if it["level"] >= 3 else 1
        if not after_ai:
            it["why"] += ", low-tier outlet"
    elif tier == 1:
        it["untrusted"] = True
        it["level"] = max(1, it["level"] - 1)
        if not after_ai:
            it["why"] += ", unfamiliar outlet"


def page_threshold(it) -> int:
    return 3 if it.get("untrusted") else 2


BLOCKED_SOURCES = re.compile(r"openpr|ein presswire|marketscreener|benzinga|globe newswire|"
                             r"pr newswire|accesswire|einnews|newsbreak|msn$", re.I)

MAX_AGE_HOURS = float(os.environ.get("MAX_AGE_HOURS") or 3)  # only alert on news this fresh
KEEP_DAYS = 21
SIMILAR_HOURS = 7 * 24   # a story similar to one sent in the last week is old news unless it's bigger
SIMILARITY = 0.5
MAX_PER_RUN = 10

GN = "https://news.google.com/rss/search?hl=en-CA&gl=CA&ceid=CA:en&q="
BING = "https://www.bing.com/news/search?format=rss&cc=ca&setlang=en-ca&qft=sortbydate%3d%221%22&q="
UA = "Mozilla/5.0 (X11; Linux x86_64) news-alerts"

STOP = set("""a an and are as at be by for from has have in into is it its of on or that the this to was
will with after amid over new says said say could would may might about up out more than not but
canada canadian canadians ontario u s us""".split())


# ------------------------------- fetching -----------------------------------
def fetch(url: str, tries: int = 3) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, */*"})
    for n in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read()
        except Exception:
            if n == tries - 1:
                raise
            time.sleep(2 * (n + 1))
    raise RuntimeError("unreachable")


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1].lower()


def child(el, name):
    for c in el:
        if local(c.tag) == name:
            return c
    return None


def text_of(el, name) -> str:
    c = child(el, name)
    return (c.text or "").strip() if c is not None and c.text else ""


def parse_time(s: str) -> float:
    if not s:
        return time.time()
    try:
        return email.utils.parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def unwrap_bing(link: str) -> str:
    if "bing.com/news/apiclick" in link:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        if q.get("url"):
            return q["url"][0]
    return link


def parse(xml_bytes: bytes, default_source: str = ""):
    """Yield items from RSS 2.0 or Atom."""
    root = ET.fromstring(xml_bytes)
    nodes = [e for e in root.iter() if local(e.tag) in ("item", "entry")]
    for it in nodes:
        title = html.unescape(re.sub(r"<[^>]+>", "", text_of(it, "title")))
        link = text_of(it, "link")
        if not link:  # Atom: <link href="..."/>
            for c in it:
                if local(c.tag) == "link" and c.get("href") and c.get("rel", "alternate") == "alternate":
                    link = c.get("href")
                    break
        if not link and text_of(it, "id").startswith("http"):
            link = text_of(it, "id")
        link = unwrap_bing(link)
        desc = text_of(it, "description") or text_of(it, "summary")
        desc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(desc)))[:400]
        source = text_of(it, "source") or default_source
        pub = text_of(it, "pubdate") or text_of(it, "published") or text_of(it, "updated") or text_of(it, "date")
        guid = text_of(it, "guid") or text_of(it, "id") or link
        if title and link:
            yield {"title": title.strip(), "link": link, "desc": desc, "source": source,
                   "ts": parse_time(pub), "guid": guid}


def all_sources():
    for t in TOPICS:
        for i, q in enumerate(t["queries"]):
            yield {"id": f"google:{t['key']}:{i}", "name": "", "url": GN + urllib.parse.quote(q + " when:1d")}
            if i < BING_QUERIES_PER_TOPIC:
                yield {"id": f"bing:{t['key']}:{i}", "name": "", "url": BING + urllib.parse.quote(q)}
    for i, q in enumerate(OTTAWA_QUERIES):
        yield {"id": f"google:ottawa:{i}", "name": "", "url": GN + urllib.parse.quote(q + " when:1d")}
    for f in FEEDS:
        yield {**f, "id": "feed:" + f["id"]}


# ------------------------------- relevance ----------------------------------
def strip_source(t: str) -> str:
    return re.sub(r"\s+-\s+[^-]{2,60}$", "", t).strip() or t


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", strip_source(t).lower()).strip()


def story_words(it) -> set:
    # For tracked items (bills), the stage is part of what makes a story new.
    return words(it["title"] + (" " + it["desc"] if it.get("track_changes") else ""))


def words(t: str) -> set:
    return {w for w in norm_title(t).split() if len(w) > 2 and w not in STOP}


def similar(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    if min(len(a), len(b)) >= 4:
        shared = len(a & b)
        return shared / min(len(a), len(b)) if shared >= 3 else 0.0
    return len(a & b) / len(a | b)


def same_story(a: set, b: set) -> bool:
    """Pre-filter before rating. With Claude available it decides what's a repeat and what's a new
    development, so only near-identical headlines are dropped here; without it, use the looser word match."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return bool(a and b) and len(a & b) / len(a | b) >= 0.7
    return similar(a, b) >= SIMILARITY


def snippet(item) -> str:
    """The item's summary, if it says more than the headline (Google News summaries usually don't)."""
    d = item.get("desc", "").strip()
    if not d:
        return ""
    if len(words(d) - words(item["title"]) - words(item.get("source", ""))) < 5:
        return ""
    d = re.sub(r"\s*(The post .* appeared first on .*|Continue reading.*|Read more.*)$", "", d)
    return d if len(d) <= 220 else d[:217].rsplit(" ", 1)[0] + "…"


TOPIC_BY_KEY = {}


def classify(item):
    if item.get("force_topic"):
        if not TOPIC_BY_KEY:
            TOPIC_BY_KEY.update({t["key"]: t for t in TOPICS})
        return TOPIC_BY_KEY.get(item["force_topic"])
    # Headline + summary only: outlet names like "Automotive News Canada" would
    # otherwise make every story from that outlet look Canadian and auto-related.
    text = item["title"] if item.get("title_only") else f"{item['title']} {item['desc']}"
    if item.get("context"):
        text += " " + item["context"]
    for t in TOPICS:
        if all((r.search(text) if isinstance(r, re.Pattern) else re.search(r, text, re.I)) for r in t["require"]):
            return t
    return None


def rule_score(item) -> tuple[int, str]:
    t, title = item["topic"], strip_source(item["title"])
    if item.get("track_changes"):  # a bill's stage ("Royal assent…") is the news
        title += " " + item["desc"]
    score, why = t["base"], []
    assoc = t["assoc"]
    if re.search(ASSOCS[assoc]["name_re"], title):
        score += 1
        why.append(f"names {assoc}")
    m = MAJOR.search(title)
    major = bool(m)
    if m:
        score += 2
        why.append(f"'{m.group(0).lower()}'")
    else:
        m = MINOR.search(title)
        if m:
            score += 1
            why.append(f"'{m.group(0).lower()}'")
    if item.get("force_topic"):   # a mention in Parliament: notable at most without Claude's read of it
        return min(3, score), "mentioned in Parliament"
    if item.get("official"):
        score += 1
        why.append("official source")
    if SOFT.search(title):
        score -= 1
        why.append("opinion/feature")
    # Level 5 is reserved for an actual decision/event, not a pile of minor signals.
    return max(1, min(5 if major else 4, score)), ", ".join(why) or "on-topic"


# ---------------------------- Claude scoring --------------------------------
AI_SYSTEM = """You rate news headlines for someone in Ottawa government relations who follows two \
associations. Each headline is tagged with the one it was matched to; rate it for that association.

CADA - Canadian Automobile Dealers Association, the national body for Canada's franchised new-car \
dealers. They want to know fast about anything that changes conditions for Canadian dealers: CADA itself; Canada-US-Mexico trade \
(CUSMA/USMCA and its review, tariffs on vehicles, parts, steel and aluminum, countermeasures, Chinese \
EV tariffs); auto plants in Canada being built, expanded, retooled, idled, closed, sold or moved; \
federal and provincial EV rules and rebates; vehicle supply, prices, sales, financing, theft and \
recalls; dealer regulation.

HEAL - Organizations for Health Action, a coalition of 43 national health organizations \
(provider associations and health charities) that lobbies the federal government. They care about federal \
health policy broadly. As an extra signal (not the full picture, and somewhat dated), HEAL's January 2025 \
consensus document highlighted: the health human resources crisis (workforce plan and data, accreditation of \
internationally trained professionals, interprovincial mobility, burnout); mental health parity (the promised \
Canada Mental Health Transfer, parity legislation); and community-based care (team-based care, home and \
community care, caregivers, palliative care, children's health, virtual care). Treat stories on those as \
relevant, but never rate a story lower because it falls outside them. \
They want to know fast about: HEAL itself or its member organizations; federal health funding (Canada Health Transfer, bilateral deals, federal budgets); the \
health workforce (shortages, national licensure, labour mobility, internationally educated professionals, \
retention); pharmacare, dental care, drug shortages and drug pricing; the Canada Health Act (private clinics, \
user fees); health data and digital health; primary care and access; aging, long-term and home care; \
mental health and substance use; public health; Indigenous health; AI in health care (AI scribes and \
ambient dictation, clinical AI, privacy and rules for health AI), including MP Michael Ma's AI initiatives \
and the legislation he is expected to lead; the health minister, Health Canada, \
Parliament's health committees, federal-provincial health ministers, and health bills.

Rate each headline 1-5 for its association:
5 = a decision or event that changes things now (for HEAL, Michael Ma tabling his AI legislation counts). CADA: tariffs imposed or lifted, a CUSMA deal or \
breakdown, an EV mandate scrapped or changed, a major Canadian plant closing or moving. HEAL: a federal \
budget or funding deal, a health bill passing, pharmacare/dental changes, a new health minister. Either: \
the association itself in a major story.
4 = significant development: key talks or deadlines, big investments or layoffs, new rebates or funding \
announcements, committee hearings or consultations, statements by the association.
3 = notable update worth knowing today.
2 = routine or background: monthly data, minor company news, features.
1 = not relevant to that association (wrong country or sector, consumer reviews, lifestyle, spam).
Judge from the headline and snippet only; don't assume facts beyond them. Several items may cover the \
same event: rate each on its own.
Ongoing stories: you're told today's date, when each item was published, and the stories already on \
file (S0, S1, ...) with their latest headlines. For each item:
- "thread": the number of the on-file story it belongs to, or -1. One story = one ongoing matter, e.g. every \
article about the possible sale of the Stellantis Brampton plant is one story; "Canada-U.S. trade talks" is one story.
- "same_as": if it belongs to no on-file story but another item EARLIER in this list is about the same matter, \
that item's id; otherwise -1.
- "development": true only if it reports a genuinely new step in the story (a decision, deal, vote, announcement, \
filing, new numbers) that the on-file headlines don't already cover. False for reaction, commentary, analysis, \
recaps, or another outlet's version of what's on file. For an item that starts a new story, true.
- "story": a 2-6 word name for the story (reuse the on-file name when thread >= 0).
Rate "level" on the item's own news value; being a repeat is expressed by "development": false, not by a low level. \
Rate at most 2 if it's a recap, explainer, anniversary or look-back, or reports something that happened well \
before today (e.g. it refers to last month, last year or an earlier date).
Items from "Parliament (Hansard)" are remarks made in the House or a committee: rate by what was said about the \
association's issues (a passing mention is 2; substantive debate, testimony or a minister's commitment is 3-4).
"summary": one or two plain sentences (max 40 words) saying what happened, using only facts in the headline and snippet; never invent numbers, dates or quotes.
"why": at most 15 words on what it means for that association's members (for CADA: prices, supply, \
demand, costs, rules for dealers; for HEAL: funding, workforce, access to care, federal policy)."""

AI_SCHEMA = {
    "type": "object",
    "properties": {"ratings": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "level": {"type": "integer"},
                       "summary": {"type": "string"}, "why": {"type": "string"},
                       "thread": {"type": "integer"}, "same_as": {"type": "integer"},
                       "development": {"type": "boolean"}, "story": {"type": "string"}},
        "required": ["id", "level", "summary", "why", "thread", "same_as", "development", "story"],
        "additionalProperties": False}}},
    "required": ["ratings"], "additionalProperties": False,
}


def item_id(it) -> str:
    return hashlib.sha1((it.get("link") or it["title"]).encode()).hexdigest()[:16]


def known_stories(news_items, now, days=5, limit=40):
    """Stories already on the news page, newest first: [{tid, story, titles: [(title, seen)]}]."""
    by = {}
    for i in sorted(news_items, key=lambda i: -i["seen"]):
        if now - i["seen"] > days * 86400:
            continue
        g = by.setdefault(i.get("thread") or i["id"], {"tid": i.get("thread") or i["id"], "story": "", "titles": []})
        g["story"] = g["story"] or i.get("story") or ""
        if len(g["titles"]) < 3:
            g["titles"].append((i["title"], i["seen"]))
    out = list(by.values())[:limit]
    for g in out:
        g["story"] = g["story"] or g["titles"][0][0][:60]
    return out


def ai_score(items, already_sent=(), stories=()) -> bool:
    """Set item['level'] / item['why'] using Claude. Returns False if unavailable."""
    if not items or not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic
    except ImportError:
        print("anthropic package not installed; using rules", file=sys.stderr)
        return False
    now = time.time()

    def ago(ts):
        m = max(0, int((now - ts) / 60))
        return f"{m} min ago" if m < 120 else f"{m // 60} h ago"
    lines = [f"{i}. [{it['topic']['assoc']} / {it['topic']['label']}] {strip_source(it['title'])} "
             f"({it['source'] or 'unknown'}, published {ago(it.get('ts', now))})"
             + (f" - {snippet(it)[:300]}" if snippet(it) else "")
             for i, it in enumerate(items)]
    today = datetime.now().strftime("%A, %B %d, %Y")
    prompt = f"Today is {today}.\n\n"
    if stories:
        prompt += "Stories on file (newest first):\n" + "\n".join(
            f"S{n}: {g['story']} | " + " | ".join(f"{t} ({ago(ts)})" for t, ts in g["titles"])
            for n, g in enumerate(stories)) + "\n\n"
    elif already_sent:
        prompt += "Headlines already sent in the last week (treat each as a story on file with no number; " \
                  "use thread -1 and development false for repeats):\n" + "\n".join(
            f"- {t} ({ago(ts)})" for t, ts in already_sent[:40]) + "\n\n"
    prompt += "Rate these headlines:\n" + "\n".join(lines)
    model = os.environ.get("CLAUDE_MODEL", "").strip() or "claude-opus-5-5"
    try:
        client = anthropic.Anthropic(timeout=60, max_retries=2)
        resp = client.beta.messages.create(
            model=model,
            max_tokens=8000,
            system=AI_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": AI_SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError:
        print("Claude: invalid ANTHROPIC_API_KEY; using rules", file=sys.stderr)
        return False
    except anthropic.RateLimitError:
        print("Claude: rate limited; using rules", file=sys.stderr)
        return False
    except anthropic.APIStatusError as e:
        print(f"Claude: API error {e.status_code}; using rules", file=sys.stderr)
        return False
    except anthropic.APIConnectionError:
        print("Claude: connection error; using rules", file=sys.stderr)
        return False
    except Exception as e:  # e.g. an older SDK without a parameter: never let scoring break alerts
        print(f"Claude: {type(e).__name__}: {e}; using rules", file=sys.stderr)
        return False
    if resp.stop_reason in ("refusal", "max_tokens"):
        print(f"Claude: stopped ({resp.stop_reason}); using rules", file=sys.stderr)
        return False
    try:
        text = next(b.text for b in resp.content if b.type == "text")
        ratings = json.loads(text)["ratings"]
    except (StopIteration, ValueError, KeyError):
        print("Claude: unreadable reply; using rules", file=sys.stderr)
        return False
    for r in ratings:
        i = r.get("id")
        if isinstance(i, int) and 0 <= i < len(items):
            items[i]["level"] = max(1, min(5, int(r["level"])))
            items[i]["why"] = r.get("why", "").strip()[:160]
            items[i]["summary"] = r.get("summary", "").strip()[:320]
            items[i]["by_ai"] = True
            items[i]["development"] = bool(r.get("development", True))
            items[i]["story"] = (r.get("story") or "").strip()[:60]
            th, sa = r.get("thread", -1), r.get("same_as", -1)
            if isinstance(th, int) and 0 <= th < len(stories):
                items[i]["thread"] = stories[th]["tid"]
                items[i]["story"] = stories[th]["story"] or items[i]["story"]
                items[i]["joined"] = True
            elif isinstance(sa, int) and 0 <= sa < i:
                items[i]["same_as_idx"] = sa
    for i, it in enumerate(items):   # resolve "same as an earlier item in this batch"
        if it.get("by_ai") and not it.get("thread"):
            sa = it.pop("same_as_idx", None)
            if sa is not None and items[sa].get("thread"):
                it["thread"], it["joined"] = items[sa]["thread"], True
                it["story"] = items[sa].get("story") or it["story"]
            else:
                it["thread"] = item_id(it)
    print(f"Claude ({model}) rated {len(ratings)} headlines")
    return True


# -------------------------------- output ------------------------------------
def send(server, topic, title, message, click="", priority=3, tags=None):
    body = {"topic": topic, "title": title[:250], "message": message[:3500], "priority": priority}
    if click:
        body["click"] = click
        body["actions"] = [{"action": "view", "label": "Open", "url": click}]
    if tags:
        body["tags"] = tags
    if os.environ.get("DRY_RUN") == "1":
        print("WOULD SEND:", json.dumps(body, ensure_ascii=False))
        return
    req = urllib.request.Request(server.rstrip("/") + "/", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def load_state(path: str) -> dict:
    try:
        with open(path) as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    s.setdefault("seen", {})
    s.setdefault("sent", [])
    s.setdefault("seeded", ["cada"] if s["seen"] else [])
    return s


# ------------------------------ news page data -------------------------------
# Every rated story (not just the ones sent as notifications) is kept for a week in
# news.json, which the workflow publishes to the "news-data" branch for the news page.
PAGE_URL = "https://ethanlaneday.github.io/news-alerts/"
# Tapping an alert opens PAGE_URL?go=<id>: the page notes the story as seen on this small ntfy
# channel and forwards to the article. Each run collects those notes into news.json ("tapped"),
# so the news app shows the story as seen on every device. The channel only ever carries story ids.
SEEN_TOPIC = os.environ.get("SEEN_TOPIC", "").strip() or "news-seen-57d844c41359b1b3"
NEWS_KEEP_DAYS = 7
BRIEF_EVERY_MIN = 60   # refresh Claude's "Today in brief" at most this often

BRIEF_SYSTEM = """You write a short morning-briefing style summary for someone in Ottawa government \
relations who follows two associations: CADA (Canadian Automobile Dealers Association) and HEAL \
(Organizations for Health Action, a coalition of national health organizations). You get today's \
headlines with their importance (5 = critical, 2 = FYI). For each association write one or two plain \
sentences on what matters most today, most important first. Use only facts in the headlines and \
summaries; never invent numbers, names or dates. If an association has nothing notable, say so in a few words."""

BRIEF_SCHEMA = {"type": "object", "properties": {"CADA": {"type": "string"}, "HEAL": {"type": "string"}},
                "required": ["CADA", "HEAL"], "additionalProperties": False}


def load_news(path: str) -> dict:
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("items", [])
    d.setdefault("brief", None)
    return d


def story_record(it, now) -> dict:
    t = it["topic"]
    return {
        "id": item_id(it), "thread": it.get("thread") or item_id(it), "story": it.get("story", ""),
        "dev": bool(it.get("development", True)),
        "assoc": t["assoc"], "topic": t["label"], "level": it["level"],
        "title": strip_source(it["title"]), "source": it["source"], "link": it["link"],
        "published": int(it["ts"]), "seen": int(now),
        "summary": (it.get("summary") if it.get("by_ai") else snippet(it)) or "",
        "why": it["why"] if it.get("by_ai") else "",
        "signals": "" if it.get("by_ai") else it["why"],
    }


def write_brief(items, now):
    """Claude's two-line brief of the last 24 hours, or None if unavailable."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None
    recent = sorted((i for i in items if now - i["seen"] < 86400 and i["level"] >= 3),
                    key=lambda i: (-i["level"], -i["seen"]))[:40]
    if not recent:
        return None
    lines = [f"- [{i['assoc']} / {i['topic']} / level {i['level']}] {i['title']} ({i['source']})"
             + (f" - {i['summary'][:200]}" if i["summary"] else "") for i in recent]
    model = os.environ.get("CLAUDE_MODEL", "").strip() or "claude-opus-5-5"
    try:
        client = anthropic.Anthropic(timeout=60, max_retries=2)
        resp = client.beta.messages.create(
            model=model, max_tokens=4000, system=BRIEF_SYSTEM,
            messages=[{"role": "user", "content": "Today's headlines:\n" + "\n".join(lines)}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": BRIEF_SCHEMA}},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        if resp.stop_reason in ("refusal", "max_tokens"):
            return None
        d = json.loads(next(b.text for b in resp.content if b.type == "text"))
        return {"at": int(now), "CADA": d["CADA"].strip()[:600], "HEAL": d["HEAL"].strip()[:600]}
    except Exception as e:  # the brief is a nice-to-have; never let it break alerts
        print(f"brief: {type(e).__name__}: {e}", file=sys.stderr)
        return None


THREAD_SYSTEM = """You group news headlines into ongoing stories for a news page. One story = one ongoing \
matter (e.g. every article about the possible sale of the Stellantis Brampton plant; or Canada-U.S. trade talks). \
Return only groups of two or more headlines that are clearly the same matter, each with a 2-6 word name. \
Don't group headlines that merely share a broad topic."""
THREAD_SCHEMA = {"type": "object", "properties": {"groups": {"type": "array", "items": {
    "type": "object", "properties": {"story": {"type": "string"}, "ids": {"type": "array", "items": {"type": "integer"}}},
    "required": ["story", "ids"], "additionalProperties": False}}}, "required": ["groups"], "additionalProperties": False}


def rethread(news) -> int:
    """Give stories saved before threads existed a thread, using Claude. Returns how many were grouped."""
    legacy = [i for i in news["items"] if not i.get("thread")]
    for i in legacy:
        i["thread"], i["story"], i["dev"] = i["id"], "", True
    if len(legacy) < 2 or not os.environ.get("ANTHROPIC_API_KEY"):
        return 0
    try:
        import anthropic
        legacy.sort(key=lambda i: i["seen"])
        lines = [f"{n}. [{i['assoc']} / {i['topic']}] {i['title']}" for n, i in enumerate(legacy[:200])]
        resp = anthropic.Anthropic(timeout=90, max_retries=2).beta.messages.create(
            model=os.environ.get("CLAUDE_MODEL", "").strip() or "claude-opus-5-5", max_tokens=8000,
            system=THREAD_SYSTEM, messages=[{"role": "user", "content": "Headlines:\n" + "\n".join(lines)}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": THREAD_SCHEMA}},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        if resp.stop_reason in ("refusal", "max_tokens"):
            return 0
        groups = json.loads(next(b.text for b in resp.content if b.type == "text"))["groups"]
    except Exception as e:
        print(f"rethread: {type(e).__name__}: {e}", file=sys.stderr)
        return 0
    grouped, used = 0, set()
    for g in groups:
        ids = [n for n in g.get("ids", []) if isinstance(n, int) and 0 <= n < len(legacy) and n not in used]
        if len(ids) < 2:
            continue
        used.update(ids)
        head = legacy[min(ids)]
        for k, n in enumerate(sorted(ids)):
            legacy[n]["thread"], legacy[n]["story"] = head["id"], (g.get("story") or "").strip()[:60]
            legacy[n]["dev"] = k == 0
        grouped += len(ids)
    return grouped


def fetch_taps() -> dict:
    """Story ids tapped from notifications in the last few hours: {id: time}."""
    taps = {}
    try:
        raw = fetch(f"https://ntfy.sh/{SEEN_TOPIC}/json?poll=1&since=6h", tries=1).decode("utf-8", "replace")
    except Exception as e:
        print(f"seen sync: {type(e).__name__}: {e}", file=sys.stderr)
        return taps
    for line in raw.splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        for sid in re.findall(r"\b[0-9a-f]{16}\b", str(m.get("message", ""))):
            taps[sid] = int(m.get("time") or time.time())
    return taps


def merge_taps(news):
    if os.environ.get("FEED_FILE") or os.environ.get("DRY_RUN") == "1":
        return
    ids = {i["id"] for i in news["items"]}
    tapped = {k: v for k, v in (news.get("tapped") or {}).items() if k in ids}
    new = {k: v for k, v in fetch_taps().items() if k in ids and k not in tapped}
    tapped.update(new)
    news["tapped"] = tapped
    if new:
        print(f"seen sync: {len(new)} stories marked seen from notification taps")


def update_news(path, cands, now, force_brief=False):
    threshold_for_page = page_threshold
    news = load_news(path)
    have = {i["id"] for i in news["items"]}
    added = added_high = 0
    for it in cands:
        if it["level"] < threshold_for_page(it):
            continue
        rec = story_record(it, now)
        if rec["id"] not in have:
            news["items"].append(rec)
            have.add(rec["id"])
            added += 1
            added_high += rec["level"] >= 4
    news["items"] = sorted((i for i in news["items"] if now - i["seen"] < NEWS_KEEP_DAYS * 86400),
                           key=lambda i: -i["seen"])[:1500]
    old = news.get("brief") or {}
    age = now - old.get("at", 0)
    # Refresh the brief hourly when there's news, within 10 min of a HIGH/CRITICAL story.
    if force_brief or not old or (added and age >= BRIEF_EVERY_MIN * 60) or (added_high and age >= 600):
        b = write_brief(news["items"], now)
        if b:
            news["brief"] = b
    merge_taps(news)
    news["checked"] = int(now)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(news, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)
    print(f"news page: +{added} stories, {len(news['items'])} kept")
    return news


def backfill(server, topic, feed_override):
    """Fill the news page with recent stories (default: last 72 h) without sending any alerts.
    Used once after the page went live, since earlier stories were only recorded, not kept."""
    path = os.environ.get("NEWS_FILE", "").strip()
    if not path:
        print("backfill needs NEWS_FILE (runs on main only)", file=sys.stderr)
        return 1
    hours = float(os.environ.get("BACKFILL_HOURS") or 72)
    now = time.time()
    found, titles = [], set()
    for src, items, err in read_all(feed_override):
        if err:
            print(f"[{src['id']}] failed: {err}", file=sys.stderr)
            continue
        for it in items:
            t = classify(it)
            if not t or (it["source"] and BLOCKED_SOURCES.search(it["source"])):
                continue
            if not (now - hours * 3600 <= it["ts"] <= now + 3600):
                continue
            key = norm_title(it["title"])
            if key in titles:
                continue
            titles.add(key)
            it["topic"] = t
            found.append(it)
    for it in found:
        it["level"], it["why"] = rule_score(it)
        apply_outlet_rules(it)
    found.sort(key=lambda i: (-i["level"], -outlet_tier(i), -i["ts"]))
    kept, cands = [], []
    for it in found:  # one story per event, the best-rated version first
        w = story_words(it)
        if not it.get("track_changes") and any(same_story(w, prev) for prev in kept):
            continue
        kept.append(w)
        cands.append(it)
    cands = cands[:240]
    news = load_news(path)
    regrouped = rethread(news)
    have = {i["id"] for i in news["items"]}
    cands = [it for it in cands if item_id(it) not in have]
    stories = known_stories(news["items"], now, days=7, limit=60)
    for i in range(0, len(cands), 40):  # Claude rates, summarises and threads in batches
        batch = cands[i:i + 40]
        if ai_score(batch, stories=stories):
            for it in batch:
                if it.get("by_ai"):
                    apply_outlet_rules(it, after_ai=True)
                    if not it.get("joined"):   # let later batches join stories started in this one
                        stories.append({"tid": it["thread"], "story": it.get("story") or it["title"][:60],
                                        "titles": [(strip_source(it["title"]), it["ts"])]})
    added = 0
    for it in cands:
        if it["level"] < page_threshold(it):
            continue
        rec = story_record(it, now)
        rec["seen"] = int(min(now, it["ts"]))  # place it on the page when it was published
        if rec["id"] not in have:
            news["items"].append(rec)
            have.add(rec["id"])
            added += 1
    news["items"] = sorted((i for i in news["items"] if now - i["seen"] < NEWS_KEEP_DAYS * 86400),
                           key=lambda i: -i["seen"])[:1500]
    b = write_brief(news["items"], now)
    if b:
        news["brief"] = b
    merge_taps(news)
    news["checked"] = int(now)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(news, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    print(f"backfill: {len(found)} relevant, {len(cands)} new and distinct, +{added} added to the page, "
          f"{regrouped} older stories grouped into threads")
    send(server, topic, "News page updated",
         f"Added {added} stories from the last {int(hours)} hours to the news page. No alerts were sent for them.",
         click=PAGE_URL, priority=2, tags=["newspaper"])
    return 0


def save_state(path: str, state: dict):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f)


def read_source(src, feed_override):
    data = open(feed_override, "rb").read() if feed_override else fetch(src["url"])
    items = list(parse(data, src.get("name", "")))
    for it in items:
        if src.get("official"):
            it["official"] = True
        if src.get("title_only"):
            it["title_only"] = True
        if src.get("context"):
            it["context"] = src["context"]
        if src.get("track_changes"):
            it["track_changes"] = True
        if src.get("max_age_hours"):
            it["max_age"] = src["max_age_hours"]
        if src.get("parl"):
            # Feed titles look like "Business of Supply / Jane Doe" or " / Jane Doe".
            m = re.match(r"^(.*?)\s*/\s*(.+)$", it["title"])
            ctx, who = (m.group(1).strip(), m.group(2).strip()) if m else ("", it["title"].strip() or "A speaker")
            day = datetime.fromtimestamp(it["ts"]).strftime("%b %-d")
            it["title"] = f"In Parliament, {day}: {who} on {src['parl']}" + (f" ({ctx})" if ctx else "")
            it["force_topic"] = src["topic"]
        if src["id"].startswith("feed:"):
            # Only search results end in " - Outlet"; keep direct-feed titles whole
            # (e.g. "C-64 (45-1) - An Act respecting pharmacare").
            it["title"] = it["title"].replace(" - ", " – ")
    return items


def read_all(feed_override):
    """Fetch every source at once (much faster than one by one). Yields (src, items, error) in order."""
    from concurrent.futures import ThreadPoolExecutor
    sources = list(all_sources())

    def one(src):
        try:
            return src, read_source(src, feed_override), None
        except Exception as e:  # one bad source shouldn't stop the rest
            return src, [], e
    with ThreadPoolExecutor(max_workers=8) as pool:
        yield from pool.map(one, sources)


def probe(urls):
    """Diagnostic: fetch each URL from the runner and print what comes back."""
    for u in urls.split():
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                body = resp.read(400000)
                text = body.decode("utf-8", "replace")
                links = sorted(set(re.findall(r'(?:href|src)="([^"]*(?:rss|feed|atom|xml)[^"]*)"', text, re.I)))[:15]
                print(f"PROBE {resp.status} {resp.headers.get('Content-Type')} len={len(body)} {u}")
                print("   head:", re.sub(r"\s+", " ", text[:500]))
                print("   titles:", [html.unescape(t)[:90] for t in re.findall(r"<title[^>]*>([^<]{3,})", text)[:6]])
                print("   feed-like links:", links)
        except Exception as e:
            print(f"PROBE FAIL {u} :: {type(e).__name__}: {e}")
    return 0


def check_feeds(server, topic, feed_override):
    rows, bad, quiet, audit = [], [], [], []
    verbose = os.environ.get("CHECK_VERBOSE") == "1"
    for src, items, err in read_all(feed_override):
        try:
            if err:
                raise err
            rel = sum(1 for it in items if classify(it))
            rows.append(f"OK   {src['id']:<28} {len(items):>3} items, {rel} relevant")
            if src["id"].startswith("feed:") and (verbose or src["id"].startswith("feed:parl-")):
                for it in items[:2]:
                    rows.append(f"       e.g. {it['title'][:110]!r} | {it['desc'][:110]!r}")
                if not items and not feed_override:
                    try:
                        rows.append(f"       raw: {fetch(src['url'])[:300]!r}")
                    except Exception as e:
                        rows.append(f"       raw fetch failed: {e}")
            if not items:
                quiet.append(src["id"])
                if src["id"].startswith("google:") and not feed_override:
                    # Quiet day, or a search Google can't parse? Try the same search over a week.
                    try:
                        wk = list(parse(fetch(src["url"].replace("when%3A1d", "when%3A7d"))))
                        q = urllib.parse.unquote(src["url"].split("&q=", 1)[1])
                        audit.append((len(wk), src["id"], q.replace(" when:1d", "")))
                    except Exception as e:
                        rows.append(f"       7-day retry failed: {e}")
        except Exception as e:
            rows.append(f"FAIL {src['id']:<28} {e}")
            bad.append(src["id"])
    print("\n".join(rows))
    if audit:
        print("SEARCH AUDIT: Google searches with nothing in the last day, and how many they find over 7 days")
        for n, sid, q in sorted(audit):
            print(f"  {n:>3} in 7 days | {sid:<26} | {q}")
    total = sum(1 for row in rows if row.startswith(("OK ", "FAIL ")))
    msg = f"{total - len(bad)}/{total} sources working."
    if bad:
        msg += "\nNot working: " + ", ".join(bad)
    if quiet:
        msg += f"\n{len(quiet)} searches had no news in the last day (normal)."
    ai = "off (rules only)"
    if os.environ.get("ANTHROPIC_API_KEY"):
        # One sample headline proves the key, model and credit all work (costs a fraction of a cent).
        sample = {"title": "Ottawa and Washington reach deal to lift auto tariffs under CUSMA review",
                  "desc": "", "source": "Test", "topic": TOPICS[1], "ts": time.time()}
        model = os.environ.get("CLAUDE_MODEL", "").strip() or "claude-opus-5-5"
        if ai_score([sample]):
            ai = f"working ({model}) - sample rated {sample['level']}: {sample.get('summary', '')[:120]}"
        else:
            ai = f"key set but the test call failed ({model}) - see the run log"
    send(server, topic, "News alerts: source check", msg + f"\nClaude scoring: {ai}", priority=2, tags=["mag"])  # quiet: no sound or banner
    return 0


def main() -> int:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic and os.environ.get("DRY_RUN") != "1":
        print("NTFY_TOPIC is not set (add it as a repository secret).", file=sys.stderr)
        return 1
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
    path = os.environ.get("STATE_FILE", ".cada-state/seen.json")
    feed_override = os.environ.get("FEED_FILE")
    min_level = int(os.environ.get("MIN_LEVEL", "2") or 2)

    if os.environ.get("SEND_TEST") == "1":
        for assoc, label in (("CADA", "Trade/CUSMA"), ("HEAL", "Health funding")):
            send(server, topic, f"{assoc} · HIGH · Test alert: what an important {assoc} story looks like",
                 f"{label} · Test · just now\nSignals: test. If this popped up with a sound, alerts are working.",
                 click=PAGE_URL, priority=4,
                 tags=[ASSOCS[assoc]["emoji"], "red_circle"])
        return 0
    if os.environ.get("PROBE", "").strip():
        return probe(os.environ["PROBE"])
    if os.environ.get("BACKFILL") == "1":
        return backfill(server, topic, feed_override)
    if os.environ.get("CHECK_FEEDS") == "1":
        return check_feeds(server, topic, feed_override)

    state = load_state(path)
    seen: dict = state["seen"]
    seeded = set(state["seeded"])
    now = time.time()
    # Each entry: [words, time sent, level, headline]. Older state has only the first two.
    sent_recent = [(set(e[0]), e[1], e[2] if len(e) > 2 else 3, e[3] if len(e) > 3 else "")
                   for e in state["sent"] if now - e[1] < SIMILAR_HOURS * 3600]

    fresh, ok, total, new_seeds = [], 0, 0, []
    for src, items, err in read_all(feed_override):
        total += 1
        if err:
            print(f"[{src['id']}] failed: {err}", file=sys.stderr)
            continue
        ok += 1
        src_seeding = src["id"].startswith("feed:") and src["id"] not in seeded
        if src_seeding:
            new_seeds.append(src["id"])
        for it in items:
            t = classify(it)
            if not t:
                continue
            if it["source"] and BLOCKED_SOURCES.search(it["source"]):
                continue
            keys = ["g:" + it["guid"], "t:" + norm_title(it["title"])]
            if it.get("track_changes"):  # key on the latest activity, not just the bill
                keys = [k + "|" + norm_title(it["desc"]) for k in keys]
            if any(k in seen for k in keys):
                continue
            for k in keys:
                seen[k] = now
            if src_seeding or t["key"] not in seeded or now - it["ts"] > it.get("max_age", MAX_AGE_HOURS) * 3600:
                continue
            it["topic"] = t
            fresh.append(it)

    if total and ok == 0:
        return 1  # everything failed; keep old state, retry next run

    first_topics = [t for t in TOPICS if t["key"] not in seeded]
    for t in first_topics:
        seeded.add(t["key"])
    seeded.update(new_seeds)
    state["seeded"] = sorted(seeded)

    # Score every fresh story, then keep one per event, preferring the most important
    # story from the best outlet (so a junk site can't "claim" a story first).
    def threshold(it):
        return max(min_level, 3 if it.get("untrusted") else 1)

    for it in fresh:
        it["level"], it["why"] = rule_score(it)
        apply_outlet_rules(it)
    fresh.sort(key=lambda i: (-i["level"], -outlet_tier(i), i["ts"]))
    cands, kept = [], []
    for it in fresh:
        w = story_words(it)
        prior = [] if it.get("track_changes") else \
            [lvl for prev, _, lvl, _ in sent_recent + kept if same_story(w, prev)]
        # Same story as one already sent (this week) is old news, unless it's a bigger development.
        if prior and not (it["level"] >= 4 and it["level"] > max(prior)):
            print("  already covered, skipped:", it["title"])
            continue
        kept.append((w, now, it["level"], strip_source(it["title"])))
        cands.append(it)

    news_path = os.environ.get("NEWS_FILE", "").strip()
    already = [(t, ts) for _, ts, _, t in sorted(sent_recent, key=lambda e: -e[1]) if t]
    stories = known_stories(load_news(news_path)["items"], now) if news_path else []
    if cands and ai_score(cands[:40], already, stories):
        for it in cands[:40]:
            if it.get("by_ai"):
                apply_outlet_rules(it, after_ai=True)
                # Another article on a story already on file, with nothing new: page only, no alert.
                if not it.get("development", True) and (it.get("joined") or not stories):
                    it["no_alert"] = True
    keep = [it for it in cands if it["level"] >= threshold(it)]
    to_send = [it for it in keep if not it.get("no_alert")]
    for it in keep:
        if it.get("no_alert"):
            print(f"  page only (no alert): {it['title']}")
    sent_recent += [(story_words(it), now, it["level"], strip_source(it["title"])) for it in keep]

    # Quiet hours: overnight only CRITICAL alerts go out; the rest wait for the morning brief.
    local = datetime.now(ZoneInfo(os.environ.get("ALERT_TZ", "").strip() or "America/Toronto"))
    try:
        qs, qe = (int(x) % 24 for x in (os.environ.get("QUIET_HOURS", "").strip() or "22-8").split("-"))
    except ValueError:
        qs, qe = 22, 8
    quiet = qs != qe and ((qs <= local.hour or local.hour < qe) if qs > qe else (qs <= local.hour < qe))
    held = state.setdefault("held", [])
    if quiet:
        for it in to_send:
            if it["level"] < 5:
                held.append({"assoc": it["topic"]["assoc"], "level": it["level"], "title": strip_source(it["title"])})
                print(f"  held until morning L{it['level']}: {it['title']}")
        to_send = [it for it in to_send if it["level"] >= 5]
    for it in cands:
        if it["level"] < threshold(it):
            print(f"  level {it['level']}, not sent: {it['title']} ({it['why']})")

    if first_topics or new_seeds:
        parts = []
        if first_topics:
            by_assoc = {}
            for t in first_topics:
                by_assoc[t["assoc"]] = by_assoc.get(t["assoc"], 0) + 1
            parts.append(", ".join(f"{a} ({n} topic{'s' if n > 1 else ''})" for a, n in by_assoc.items()))
        if new_seeds:
            parts.append(f"{len(new_seeds)} new source{'s' if len(new_seeds) > 1 else ''}")
        send(server, topic, "News alerts: now watching " + " + ".join(parts),
             "Current stories were recorded silently; you'll be alerted to new ones from here.",
             tags=["white_check_mark"], priority=3)

    # Most important first, so a cap never hides the big one.
    to_send.sort(key=lambda i: (-i["level"], i["ts"]))
    for it in to_send[:MAX_PER_RUN]:
        name, tag = LEVELS[it["level"]]
        age = max(0, int((now - it["ts"]) / 60))
        when = f"{age} min ago" if age < 120 else f"{age // 60} h ago"
        assoc = it["topic"]["assoc"]
        label = it["topic"]["label"] if it["topic"]["label"] != assoc else ""
        meta = " · ".join(x for x in (label, it["source"], when) if x)
        if it.get("joined") and it.get("story"):
            meta += f"\nUpdate on: {it['story']}"
        if it.get("by_ai"):
            lines = [meta, it.get("summary", ""), "Why it matters: " + it["why"]]
        else:
            lines = [meta, snippet(it), "Signals: " + it["why"]]
        send(server, topic, f"{assoc} · {name} · {strip_source(it['title'])}", "\n".join(x for x in lines if x),
             click=PAGE_URL + "?go=" + item_id(it) if news_path else it["link"],
             priority=max(1, min(5, it["level"])), tags=[ASSOCS[assoc]["emoji"], tag])
        print(f"sent L{it['level']} [{it['topic']['key']}]:", it["title"], "|", it["why"])
    if len(to_send) > MAX_PER_RUN:
        rest = to_send[MAX_PER_RUN:]
        send(server, topic, f"+{len(rest)} more stories",
             "\n".join(f"• {i['topic']['assoc']}: " + strip_source(i["title"]) for i in rest[:8]), priority=2,
             click="https://news.google.com/search?q=CUSMA%20OR%20%22auto%20plant%22%20Canada&hl=en-CA&gl=CA&ceid=CA:en")

    # Morning brief: once a day when quiet hours end, Claude's brief plus what was held overnight.
    today = local.date().isoformat()
    morning = not quiet and state.get("morning_day") != today and local.hour >= qe \
        and (local.hour < qe + 5 or bool(held))
    news = None
    if news_path:
        try:  # the page shows everything rated FYI or higher, whatever MIN_LEVEL is
            news = update_news(news_path, cands, now, force_brief=morning)
        except Exception as e:
            print(f"news page update failed: {type(e).__name__}: {e}", file=sys.stderr)
    if morning:
        brief = (news or {}).get("brief") or {}
        parts = [f"{a}: {brief[a]}" for a in ("CADA", "HEAL") if brief.get(a) and now - brief.get("at", 0) < 3 * 3600]
        if held:
            top = sorted(held, key=lambda h: -h["level"])
            parts.append("Overnight:\n" + "\n".join(
                f"• {h['assoc']} · {LEVELS[h['level']][0]} · {h['title']}" for h in top[:8])
                + (f"\n+{len(top) - 8} more in the app" if len(top) > 8 else ""))
        if parts:
            send(server, topic, "Morning brief" + (f" · {len(held)} overnight" if held else ""),
                 "\n\n".join(parts), click=PAGE_URL, priority=3, tags=["sunrise"])
            print(f"morning brief sent ({len(held)} held stories)")
        state["held"] = []
        state["morning_day"] = today

    cutoff = now - KEEP_DAYS * 86400
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["sent"] = [[sorted(w), ts, lvl, t] for w, ts, lvl, t in sent_recent]
    save_state(path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
