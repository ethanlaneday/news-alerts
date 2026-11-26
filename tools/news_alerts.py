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
    "RVDA": {"emoji": "minibus", "name_re": r"(?i:recreation(al)? vehicle dealers|recreational dealers.? association)|\bRVDA\b"},
    "HRAI": {"emoji": "thermometer", "name_re": r"(?i:heating,? refrigeration (and|&) air conditioning institute)|\bHRAI\b"},
    "IDA": {"emoji": "cityscape", "name_re": r"(?i:international downtown association)|\bIDA Canada\b"},
}
ASSOC_ORDER = ["CADA", "HEAL", "RVDA", "HRAI", "IDA"]
RV_WORDS = re.compile(r"\bRVs?\b|(?i:recreational vehicle|recreation vehicle|motorhome|motor home|travel trailer|camper van|"
                      r"fifth.wheel|towable|rv dealer|rv industry|rv sales)")

CANADA_H = CANADA + r"|provinc|\bfpt\b|health human resources|\bcihi\b|health workforce canada|camimh"   # health news often says "provinces", not "Canada"
AI_WORDS = re.compile(
    r"(?i:artificial intelligence|\bai[- ]powered|\bai[- ]scribes?|medical scribes?|\bscribes?\b|ambient|"
    r"dictation|clinical documentation|machine learning|large language models?|\bllms?\b|chatbots?|generative)|"
    r"\bAI\b")
HEALTH = r"health|hospital|nurs|physician|doctor|pharma|drug|dental|mental health|long-term care|" \
         r"care home|patient|medic|wait time|primary care|paramedic|psycholog|therap|chiropract"

TOPICS = [
    # ---- RVDA: Recreation Vehicle Dealers Association of Canada ----
    # Specific subjects come before CADA's broader trade and market topics, so an RV or heat-pump
    # story is filed with the association it actually concerns.
    {"key": "rvda", "assoc": "RVDA", "label": "RVDA", "base": 3,
     "require": [r"recreation(al)? vehicle dealers|recreational dealers.? association|\bRVDA\b|rvda\.ca"],
     "queries": ['"RVDA of Canada" OR "Recreation Vehicle Dealers Association of Canada"', "site:rvda.ca"]},
    {"key": "rv", "assoc": "RVDA", "label": "RV industry", "base": 3,
     "require": [CANADA, RV_WORDS],
     "queries": ["Canada RV (dealers OR sales OR industry OR shipments)",
                 'Canada ("recreational vehicle" OR motorhome OR "travel trailer") (tariff OR surtax OR remission OR tax)',
                 'Canada RV (tariffs OR "luxury tax" OR financing OR prices)',
                 '"Go RVing Canada" OR "Canadian Recreational Vehicle Association" OR CRVA',
                 'Canada ("Thor Industries" OR Winnebago OR "Forest River") RV']},
    {"key": "camping", "assoc": "RVDA", "label": "Camping/tourism", "base": 2, "title_only": True,
     "require": [CANADA, r"campgrounds?|camping|campsites?|rv parks?|outdoor hospitality|parks canada"],
     "queries": ['Canada campground (tax OR "small business deduction" OR fees OR industry)',
                 "Parks Canada camping (fees OR reservations OR season)",
                 '"Canadian Camping and RV Council" OR CCRVC',
                 "Canada domestic tourism (camping OR road trips OR travel to U.S.)"]},
    # ---- HRAI: Heating, Refrigeration and Air Conditioning Institute of Canada ----
    {"key": "hrai", "assoc": "HRAI", "label": "HRAI", "base": 3,
     "require": [r"heating,? refrigeration (and|&) air conditioning institute|\bHRAI\b|hrai\.ca"],
     "queries": ['"Heating, Refrigeration and Air Conditioning Institute" OR "HRAI"', "site:hrai.ca"]},
    {"key": "heatpumps", "assoc": "HRAI", "label": "Heat pumps/retrofits", "base": 3,
     "require": [CANADA, r"heat pumps?|greener homes|home retrofits?|retrofit (program|rebate|grant|loan)|oil to heat pump|"
                         r"energy.efficien\w+ (rebate|program|retrofit|grant)|home heating|electrification of heating"],
     "queries": ['Canada "heat pump" (rebate OR program OR grant OR sales OR mandate)',
                 '"Greener Homes" OR "Oil to Heat Pump Affordability"',
                 "Canada home retrofit (program OR rebate OR funding OR loan)",
                 "Canada home heating (natural gas ban OR electrification OR costs OR oil)"]},
    {"key": "hvac", "assoc": "HRAI", "label": "HVAC rules/industry", "base": 3, "title_only": True,
     "require": [CANADA, re.compile(r"\bHVACR?\b|\bHFCs?\b|(?i:refrigerants?|hydrofluorocarbon|air conditioners?|air conditioning|"
                                    r"furnaces?|boilers?|ventilation|building codes?|energy efficiency regulations|"
                                    r"heating and cooling|refrigeration)")],
     "queries": ["Canada HVAC (industry OR contractors OR tariffs OR shortage OR regulations)",
                 "Canada refrigerant (HFC OR phase-down OR regulations)",
                 'Canada ("building code" OR "energy efficiency regulations") (heating OR cooling OR HVAC)',
                 "Canada (air conditioner OR furnace) (tariff OR surtax OR regulations OR standards)"]},
    {"key": "trades", "assoc": "HRAI", "label": "Skilled trades", "base": 2, "title_only": True,
     "require": [CANADA, r"skilled trades|apprentice|red seal|tradespeople|trades (shortage|training|workers|workforce)"],
     "queries": ["Canada skilled trades (shortage OR apprenticeship OR funding OR immigration)",
                 "Canada apprenticeship (grant OR program OR funding) federal"]},
    # ---- IDA Canada: International Downtown Association (downtowns, main streets, business districts) ----
    # These subjects are common words, so they are matched on the headline only.
    {"key": "ida", "assoc": "IDA", "label": "IDA", "base": 3,
     "require": [r"international downtown association|\bIDA Canada\b"],
     "queries": ['"International Downtown Association" OR "IDA Canada"']},
    {"key": "downtowns", "assoc": "IDA", "label": "Downtowns", "base": 3, "title_only": True,
     "require": [CANADA, re.compile(r"\bBIAs?\b|(?i:downtowns?|main streets?|business improvement (area|district|association|zone)|"
                                    r"city cent(re|er)|office vacanc|office.to.residential|office conversions?|return.to.office|"
                                    r"foot traffic|storefront)")],
     "queries": ["Canada downtown (recovery OR revitalization OR vacancy OR foot traffic)",
                 'Canada "main street" (businesses OR funding OR revitalization)',
                 '"business improvement area" OR "business improvement association" Canada',
                 "Canada office (conversion OR vacancy) downtown",
                 'federal ("return to office" OR "office buildings" OR "federal buildings") downtown',
                 "Canada downtown (safety OR disorder OR crime) businesses"]},
    {"key": "urban", "assoc": "IDA", "label": "Homelessness/safety", "base": 2, "title_only": True,
     "require": [CANADA, r"homeless|encampments?|supportive housing|open drug use|street disorder|public disorder|"
                         r"bail reform|retail (crime|theft)|shoplifting"],
     "queries": ["Canada homelessness (federal funding OR encampments OR strategy)",
                 'Canada ("bail reform" OR "retail crime") businesses',
                 "Canada encampments (city OR downtown OR court ruling)"]},
    {"key": "cities", "assoc": "IDA", "label": "Cities/transit", "base": 2, "title_only": True,
     "require": [CANADA, re.compile(r"\bFCM\b|(?i:public transit|transit fund|municipal (infrastructure|funding|finance)|"
                                    r"housing accelerator|community-building fund|federation of canadian municipalities|"
                                    r"big city mayors|build canada homes)")],
     "queries": ['Canada ("public transit fund" OR "transit funding") federal cities',
                 '"Federation of Canadian Municipalities" OR "Big City Mayors"',
                 'Canada ("Housing Accelerator Fund" OR "Build Canada Homes") cities']},
    # ---- CADA: Canadian Automobile Dealers Association ----
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
    # --- Added after a sweep for blind spots (each address was checked from the runner first) ---
    # French-language press. "lang" sends these through the French word lists in FR_TOPICS.
    {"id": "lapresse-sante", "name": "La Presse", "lang": "fr", "url": "https://www.lapresse.ca/actualites/sante/rss"},
    {"id": "lapresse-politique", "name": "La Presse", "lang": "fr", "url": "https://www.lapresse.ca/actualites/politique/rss"},
    {"id": "ledevoir-politique", "name": "Le Devoir", "lang": "fr", "url": "https://www.ledevoir.com/rss/section/politique.xml"},
    {"id": "radio-canada", "name": "Radio-Canada", "lang": "fr", "url": "https://ici.radio-canada.ca/rss/4159"},
    # United States, at the source: proclamations and notices appear here before Canadian coverage.
    {"id": "white-house", "name": "The White House", "official": True, "title_only": True,
     "keep": r"(?i)tariff|import|trade|automobile|vehicle|steel|alumin|canada|usmca|section 232|duties",
     "context": "Canada U.S. trade", "url": "https://www.whitehouse.gov/presidential-actions/feed/"},
    {"id": "fedreg-autos", "name": "U.S. Federal Register", "official": True, "kind": "fedreg", "context": "Canada U.S. trade tariff",
     "keep": r"(?i)automobile|auto parts|motor vehicle|light truck|usmca|canada|section 232|steel|alumin",
     "url": "https://www.federalregister.gov/api/v1/documents.json?conditions[term]=automobile+tariff&order=newest&per_page=20"},
    {"id": "fedreg-usmca", "name": "U.S. Federal Register", "official": True, "kind": "fedreg", "context": "Canada U.S. trade tariff",
     "keep": r"(?i)usmca|canada|automobile|motor vehicle|section 232",
     "url": "https://www.federalregister.gov/api/v1/documents.json?conditions[term]=USMCA&order=newest&per_page=20"},
    {"id": "fedreg-filed", "name": "U.S. Federal Register (filed, not yet published)", "official": True, "kind": "fedreg",
     "context": "Canada U.S. trade tariff",
     "keep": r"(?i)automobile|auto parts|motor vehicle|usmca|section 232|steel and alumin|tariff.{0,80}canad|canad.{0,80}tariff",
     "url": "https://www.federalregister.gov/api/v1/public-inspection-documents/current.json"},
    # Regulations: each issue's table of contents is read for individual orders and regulations (hourly).
    {"id": "gazette-1", "name": "Canada Gazette, Part I", "official": True, "title_only": True, "kind": "gazette",
     "context": "Canada federal regulation", "max_age_hours": 24 * 9, "url": "https://www.gazette.gc.ca/rss/p1-eng.xml"},
    {"id": "gazette-2", "name": "Canada Gazette, Part II", "official": True, "title_only": True, "kind": "gazette",
     "context": "Canada federal regulation", "max_age_hours": 24 * 16, "url": "https://www.gazette.gc.ca/rss/p2-eng.xml"},
    # Provinces
    {"id": "ontario-news", "name": "Government of Ontario", "official": True, "context": "Canada Ontario",
     "url": "https://news.ontario.ca/newsroom/en/rss/allnews.rss"},
    {"id": "bc-news", "name": "Government of British Columbia", "official": True, "context": "Canada British Columbia",
     "url": "https://news.gov.bc.ca/feed"},
    {"id": "bc-health", "name": "Government of British Columbia", "official": True, "context": "Canada British Columbia health",
     "url": "https://news.gov.bc.ca/ministries/health/feed"},
    {"id": "alberta-news", "name": "Government of Alberta", "official": True, "context": "Canada Alberta",
     "url": "https://www.alberta.ca/NewsRoom/newsroom.cfm"},
    # Stakeholders' own statements
    {"id": "cvma", "name": "Canadian Vehicle Manufacturers' Association", "primary": True, "context": "Canada auto industry vehicle",
     "url": "https://www.cvma.ca/feed/"},
    {"id": "mvro", "name": "Motor Vehicle Retailers of Ontario", "primary": True, "context": "Canada Ontario auto dealers",
     "url": "https://www.tada.ca/feed/"},
    {"id": "healthcarecan", "name": "HealthCareCAN", "primary": True, "context": "Canada health", "skip": r"\(#\w+\)|#[A-Z]{2,}\d",
     "url": "https://www.healthcarecan.ca/feed/"},
    {"id": "cfnu", "name": "Canadian Federation of Nurses Unions", "primary": True, "context": "Canada health nurses",
     "url": "https://nursesunions.ca/feed/"},
    {"id": "mhcc", "name": "Mental Health Commission of Canada", "primary": True, "context": "Canada mental health",
     "url": "https://mentalhealthcommission.ca/feed/"},
    # Federal bodies that publish outside the main Government of Canada news feed
    {"id": "pmo", "name": "Prime Minister of Canada", "official": True, "context": "Canada federal government",
     "url": "https://www.pm.gc.ca/en/news.rss"},
    {"id": "statcan-daily", "name": "Statistics Canada", "official": True, "title_only": True, "context": "Canada",
     "url": "https://www150.statcan.gc.ca/n1/rss/dai-quo/0-eng.atom"},
    {"id": "privacy-commissioner", "name": "Privacy Commissioner of Canada", "official": True, "title_only": True,
     "context": "Canada federal", "url": "https://www.priv.gc.ca/en/rss/news/"},
    {"id": "health-recalls", "name": "Health Canada recalls and advisories", "official": True, "title_only": True,
     "context": "Canada Health Canada drug health product", "url": "https://recalls-rappels.canada.ca/en/feed/health-products-alerts-recalls"},
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
    r"parliament|house of commons|senate of canada|bank of canada|health canada|canada gazette|federal register|"
    r"white house|statistics canada|privacy commissioner|prime minister of canada|government of (ontario|alberta|british columbia)", re.I)
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
    if c is None:
        return ""
    # itertext, not .text: some feeds (Statistics Canada) wrap titles in nested markup.
    return re.sub(r"\s+", " ", "".join(c.itertext())).strip()


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
        src_el = child(it, "source")
        source_url = (src_el.get("url") or "") if src_el is not None else ""
        pub = text_of(it, "pubdate") or text_of(it, "published") or text_of(it, "updated") or text_of(it, "date")
        guid = text_of(it, "guid") or text_of(it, "id") or link
        if title and link:
            yield {"title": title.strip(), "link": link, "desc": desc, "source": source, "source_url": source_url,
                   "ts": parse_time(pub), "guid": guid}


def all_sources():
    for t in TOPICS:
        # RVDA, HRAI and IDA stories usually name a town or a product, not "Canada". Their searches are
        # already limited to Canada, so results count as Canadian for the topic filters.
        extra = {"context": "Canada"} if t["assoc"] in ("RVDA", "HRAI", "IDA") else {}
        for i, q in enumerate(t["queries"]):
            yield {"id": f"google:{t['key']}:{i}", "name": "", "url": GN + urllib.parse.quote(q + " when:1d"), **extra}
            if i < BING_QUERIES_PER_TOPIC:
                yield {"id": f"bing:{t['key']}:{i}", "name": "", "url": BING + urllib.parse.quote(q), **extra}
    for i, q in enumerate(FR_QUERIES):
        yield {"id": f"google:fr:{i}", "name": "", "lang": "fr", "url": GN_FR + urllib.parse.quote(q + " when:1d")}
    for i, q in enumerate(OTTAWA_QUERIES):
        yield {"id": f"google:ottawa:{i}", "name": "", "url": GN + urllib.parse.quote(q + " when:1d")}
    for f in FEEDS:
        yield {**f, "id": "feed:" + f["id"]}


# French-language items are matched with these word lists instead of the English topic filters.
# First match wins; each entry is (topic key, pattern).
FR_AUTO = r"automobile|\bautos?\b|véhicule|voiture|concessionnaire|camionnette"
FR_PLACE = FR_AUTO + r"|acier|aluminium|canad|ottawa|washington|trump|états-unis"
FR_MAKERS = FR_AUTO + r"|stellantis|\bGM\b|general motors|\bford\b|honda|toyota|batterie|northvolt"
FR_CARE = r"santé|médecin|clinique|hôpital|patient|soins"
# Each entry: (topic key, patterns that must ALL appear, in any order).
FR_TOPICS = [(k, [re.compile(p, re.I) for p in ps]) for k, ps in [
    ("cada", [r"corporation des associations de détaillants d.automobiles|\bCADA\b"]),
    ("trade", [r"tarif|droits de douane|surtaxe|ACEUM|libre-échange|guerre commerciale|négociations commerciales", FR_PLACE]),
    ("plants", [r"usine|chaîne de montage|assemblage", FR_MAKERS]),
    ("evpolicy", [r"véhicules? électriques?|\bVZE\b|voitures? électriques?|roulez vert|bornes? de recharge|norme sur la disponibilité"]),
    ("market", [r"concessionnaires?|ventes? (de |d.)(véhicules|automobiles|voitures)|vols? (de |d.)(véhicules|voitures|autos)|rappels? (de |d.)véhicules"]),
    ("aihealth", [r"intelligence artificielle|\bIA\b|scribe", FR_CARE]),
    ("healthfunding", [r"transferts? (canadien )?en (matière de )?santé|financement (de la |des soins de |en )santé|budget (de la )?santé"]),
    ("workforce", [r"pénurie (de |d.)(infirmi|médecin|personnel|main-d.œuvre|soignant)|main-d.œuvre (en|de la) santé|"
                   r"infirmières? .*(grève|pénurie|négociation)|médecins? (formés à l.étranger|étrangers)"]),
    ("pharmacare", [r"assurance.médicaments|pénurie de médicaments|prix des médicaments|soins dentaires|régime (canadien )?de soins dentaires"]),
    ("mentalhealth", [r"santé mentale|dépendances?|opioïdes|surdoses"]),
    ("aging", [r"\bCHSLD\b|soins de longue durée|soins à domicile|proches aidants|maisons des aînés"]),
    ("access", [r"médecins? de famille|urgences|temps d.attente|première ligne|listes? d.attente"]),
    ("publichealth", [r"santé publique|vaccin|éclosion|rougeole|grippe aviaire"]),
    ("healthpolicy", [r"ministre (fédérale? )?de la santé|santé canada|loi canadienne sur la santé|aide médicale à mourir|"
                      r"système de santé|réseau de la santé|santé québec"]),
]]
FR_QUERIES = [
    "tarifs automobiles Canada États-Unis", "ACEUM révision", "usine automobile Canada (Stellantis OR GM OR Ford OR Honda OR Toyota)",
    '"véhicules électriques" Canada (norme OR rabais OR tarifs)', "concessionnaires automobiles Canada",
    '"transferts en santé" OR "Transfert canadien en matière de santé"', '"assurance médicaments" Canada',
    'pénurie (infirmières OR médecins) Canada fédéral', '"intelligence artificielle" santé Canada',
    '"ministre de la Santé" Canada fédéral', '"santé mentale" Ottawa financement',
]
GN_FR = "https://news.google.com/rss/search?hl=fr-CA&gl=CA&ceid=CA:fr&q="


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
    if item.get("lang") == "fr":
        if not TOPIC_BY_KEY:
            TOPIC_BY_KEY.update({t["key"]: t for t in TOPICS})
        return next((TOPIC_BY_KEY[k] for k, pats in FR_TOPICS if all(rx.search(text) for rx in pats)), None)
    if item.get("context"):
        text += " " + item["context"]
    head = item["title"] + (" " + item["context"] if item.get("context") else "")
    for t in TOPICS:
        where = head if t.get("title_only") else text
        if all((r.search(where) if isinstance(r, re.Pattern) else re.search(r, where, re.I)) for r in t["require"]):
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

RVDA - Recreation Vehicle Dealers Association of Canada, representing RV dealers. They want to know about: RVDA \
itself; tariffs, counter-tariffs and remission affecting RVs, trailers and RV parts; RV sales, shipments, prices and \
financing; the luxury tax and sales taxes on vehicles; campgrounds and their tax treatment; Parks Canada and camping; \
domestic tourism and cross-border travel.

HRAI - Heating, Refrigeration and Air Conditioning Institute of Canada, representing HVACR manufacturers, wholesalers \
and contractors. They want to know about: HRAI itself; heat pump and home retrofit programs (Greener Homes, Oil to Heat \
Pump Affordability) and their funding; refrigerant rules (HFC phase-down); energy-efficiency regulations and building \
codes; natural gas bans and heating electrification; tariffs on heating and cooling equipment; skilled trades and \
apprenticeship; housing construction as it affects HVAC demand.

IDA - IDA Canada, the International Downtown Association's Canadian arm, representing downtown and main-street \
business districts (BIAs). They want to know about: IDA itself; downtown recovery, office vacancy and conversions, \
federal return-to-office and federal buildings; public safety, disorder, retail crime and bail reform as they affect \
downtown businesses; homelessness and encampments; federal funding for cities, transit and housing; main-street \
small-business supports and tourism. A purely local story about one city with no wider policy angle is usually 2.

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
Some headlines are in French: rate them the same way, and write "summary", "why" and "story" in English. \
Quebec-only or other provincial health stories with no federal angle are usually 2. \
Items from the U.S. Federal Register or the White House are official U.S. actions: rate by their effect on \
Canadian autos and trade (a tariff proclamation touching vehicles, parts, steel or aluminum is 4-5; unrelated ones are 1). \
Items from the Canada Gazette are regulations and orders: a final or proposed regulation on one of the association's files is 4. \
Judge from the headline and snippet only; don't assume facts beyond them. Several items may cover the \
same event: rate each on its own.
Ongoing stories: you're told today's date, when each item was published, and the stories already on \
file (S0, S1, ...) with their latest headlines. For each item:
- "thread": the number of the on-file story it belongs to, or -1. Be strict. One story = one specific matter: \
the same main parties and the same thing at stake, e.g. every article about the possible sale of the Stellantis \
Brampton plant; or the Canada-U.S. negotiations on auto tariffs. Sharing a broad subject (trade, EVs, health \
funding), a country, a company or a politician is NOT enough: a Canada-China trade deal is not part of the \
Canada-U.S. tariff talks; Ford's Oakville investment is not part of the Stellantis Brampton sale; a second \
province's nurse shortage is not part of the first's. Ask: would the on-file headlines and this one be settled by \
the same decision or event? If not, or if unsure, use -1.
- "same_as": if it belongs to no on-file story but another item EARLIER in this list is about the same specific \
matter (same strict test), that item's id; otherwise -1.
- "development": true only if it reports a genuinely new step in the story (a decision, deal, vote, announcement, \
filing, new numbers) that the on-file headlines don't already cover. False for reaction, commentary, analysis, \
recaps, or another outlet's version of what's on file. For an item that starts a new story, true.
- "story": a 2-6 word name for the story, specific enough to tell it from its neighbours ("U.S. auto tariff talks", \
not "Trade talks"); reuse the on-file name when thread >= 0.
Rate "level" on the item's own news value; being a repeat is expressed by "development": false, not by a low level. \
Rate at most 2 if it's a recap, explainer, anniversary or look-back, or reports something that happened well \
before today (e.g. it refers to last month, last year or an earlier date).
Items from "Parliament (Hansard)" are remarks made in the House or a committee: rate by what was said about the \
association's issues (a passing mention is 2; substantive debate, testimony or a minister's commitment is 3-4).
"summary": one or two plain sentences (max 40 words) saying what happened, using only facts in the headline and snippet; never invent numbers, dates or quotes.
"why": at most 15 words on what it means for that association's members (for CADA: prices, supply, \
demand, costs, rules for dealers; for HEAL: funding, workforce, access to care, federal policy; for RVDA: RV costs, \
sales, taxes, camping demand; for HRAI: equipment rules, program funding, contractor workload, trades; for IDA: \
downtown businesses, safety, foot traffic, federal funding for cities)."""

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
        g = by.setdefault(i.get("thread") or i["id"], {"tid": i.get("thread") or i["id"], "story": "", "titles": [],
                                                       "assoc": i.get("assoc", "")})
        g["story"] = g["story"] or i.get("story") or ""
        if len(g["titles"]) < 3:
            g["titles"].append((i["title"], i["seen"]))
    out = list(by.values())[:limit]
    for g in out:
        g["story"] = g["story"] or g["titles"][0][0][:60]
    return out


# Words too common in this beat to show that two headlines are about the same matter.
GENERIC_WORDS = set("""canada canadian canadians ottawa federal government governments province provincial ontario quebec
minister ministers premier prime says said say new news plan plans deal deals talks talk trade tariff tariffs auto autos
automotive automaker automakers car cars vehicle vehicles electric industry sector health care healthcare system funding
budget workers jobs year years week month today report reports could would will after over amid with from that this
their about what how why more than into have has been were was are its his her they them and the for not but""".split())


def distinct_words(text: str) -> set:
    return {w for w in re.findall(r"[a-z][a-z0-9'-]{2,}", text.lower()) if w not in GENERIC_WORDS}


def item_text(it) -> str:
    return f"{strip_source(it['title'])} {snippet(it)}"


def fits_story(it, story) -> bool:
    """A cheap check on Claude's grouping: same association, and at least one distinctive word in common."""
    if story.get("assoc") and story["assoc"] != it["topic"]["assoc"]:
        return False
    theirs = distinct_words(story.get("story", "") + " " + " ".join(t for t, _ in story.get("titles", [])))
    return bool(distinct_words(item_text(it)) & theirs)


AI_LAST_ERROR = ""


def claude_note(msg: str):
    """Remember why Claude could not rate this run (the health check reports it if it keeps happening)."""
    global AI_LAST_ERROR
    AI_LAST_ERROR = msg[:160]
    print(f"Claude: {msg}; using rules", file=sys.stderr)


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
        claude_note("invalid API key")
        return False
    except anthropic.RateLimitError:
        claude_note("rate limited")
        return False
    except anthropic.APIStatusError as e:
        claude_note(f"API error {e.status_code}" + (" (credit may have run out)" if e.status_code in (400, 402, 403) else ""))
        return False
    except anthropic.APIConnectionError:
        claude_note("connection error")
        return False
    except Exception as e:  # e.g. an older SDK without a parameter: never let scoring break alerts
        claude_note(f"{type(e).__name__}: {e}")
        return False
    if resp.stop_reason in ("refusal", "max_tokens"):
        claude_note(f"stopped ({resp.stop_reason})")
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
            if isinstance(th, int) and 0 <= th < len(stories) and fits_story(items[i], stories[th]):
                items[i]["thread"] = stories[th]["tid"]
                items[i]["story"] = stories[th]["story"] or items[i]["story"]
                items[i]["joined"] = True
            elif isinstance(sa, int) and 0 <= sa < i and items[sa]["topic"]["assoc"] == items[i]["topic"]["assoc"] \
                    and distinct_words(item_text(items[i])) & distinct_words(item_text(items[sa])):
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
relations who follows five associations: CADA (Canadian Automobile Dealers Association), HEAL \
(Organizations for Health Action, a coalition of national health organizations), RVDA (Recreation Vehicle Dealers \
Association of Canada), HRAI (Heating, Refrigeration and Air Conditioning Institute of Canada) and IDA (IDA Canada, \
downtown and main-street business districts). You get today's \
headlines with their importance (5 = critical, 2 = FYI). For each association write one or two plain \
sentences on what matters most today, most important first. Use only facts in the headlines and \
summaries; never invent numbers, names or dates. If an association has no headlines or nothing notable, return an \
empty string for it."""

BRIEF_SCHEMA = {"type": "object", "properties": {a: {"type": "string"} for a in ASSOC_ORDER},
                "required": list(ASSOC_ORDER), "additionalProperties": False}


def load_news(path: str) -> dict:
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("items", [])
    d.setdefault("brief", None)
    return d


def outlet_domain(it) -> str:
    """The outlet's own site (for its favicon), e.g. 'cbc.ca'. Empty when only an aggregator link is known."""
    for u in (it.get("source_url"), it.get("link")):
        host = urllib.parse.urlparse(u or "").netloc.lower().removeprefix("www.")
        if host and not re.search(r"(^|\.)(google|bing|msn)\.", host):
            return host
    return ""


def story_record(it, now) -> dict:
    t = it["topic"]
    return {
        "id": item_id(it), "thread": it.get("thread") or item_id(it), "story": it.get("story", ""),
        "dev": bool(it.get("development", True)),
        "assoc": t["assoc"], "topic": t["label"], "level": it["level"],
        "title": strip_source(it["title"]), "source": it["source"], "link": it["link"], "domain": outlet_domain(it),
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
                    key=lambda i: (-i["level"], -i["seen"]))[:60]
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
        return {"at": int(now), **{a: str(d.get(a) or "").strip()[:600] for a in ASSOC_ORDER}}
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


# ---- Outlet icons, kept beside news.json so the page never depends on a third-party icon service.
OUTLET_SITES = {
    "cbc": "cbc.ca", "cbc news": "cbc.ca", "cbc.ca": "cbc.ca", "cbc windsor": "cbc.ca", "radio-canada": "radio-canada.ca",
    "ctv news": "ctvnews.ca", "cp24": "cp24.com", "global news": "globalnews.ca", "citynews": "citynews.ca",
    "globe and mail": "theglobeandmail.com", "toronto star": "thestar.com", "national post": "nationalpost.com",
    "financial post": "financialpost.com", "toronto sun": "torontosun.com", "ottawa citizen": "ottawacitizen.com",
    "windsor star": "windsorstar.com", "vancouver sun": "vancouversun.com", "montreal gazette": "montrealgazette.com",
    "calgary herald": "calgaryherald.com", "la presse": "lapresse.ca", "canadian press": "thecanadianpress.com",
    "bnn bloomberg": "bnnbloomberg.ca", "bloomberg": "bloomberg.com", "reuters": "reuters.com", "politico": "politico.com",
    "hill times": "hilltimes.com", "ipolitics": "ipolitics.ca", "national newswatch": "nationalnewswatch.com",
    "logic": "thelogic.co", "hub": "thehub.ca", "walrus": "thewalrus.ca", "tyee": "thetyee.ca", "macleans": "macleans.ca",
    "policy options": "policyoptions.irpp.org", "conversation canada": "theconversation.com", "cpac": "cpac.ca",
    "yahoo news canada": "yahoo.com", "yahoo finance canada": "yahoo.com", "rebel news": "rebelnews.com",
    "automotive news": "autonews.com", "automotive news canada": "autonews.com", "driving.ca": "driving.ca",
    "electric autonomy canada": "electricautonomy.ca", "canadian auto dealer": "canadianautodealer.ca",
    "motor1.com": "motor1.com", "ford authority": "fordauthority.com", "just auto": "just-auto.com", "cbt news": "cbtnews.com",
    "canadian automobile dealers association (cada)": "cada.ca", "rvda of canada": "rvda.ca", "hrai": "hrai.ca", "hospital news": "hospitalnews.com",
    "canadian medical association": "cma.ca", "government of canada": "canada.ca", "bank of canada": "bankofcanada.ca",
    "parliament (hansard)": "ourcommons.ca", "parliament of canada (legisinfo)": "parl.ca", "am 800 cklw": "iheartradio.ca",
}
ICONS_PER_RUN = 15
ICON_RETRY_DAYS = 7
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"


def outlet_site(rec) -> str:
    """Best guess at an outlet's site: the one recorded with the story, else a known outlet name."""
    if rec.get("domain"):
        return rec["domain"]
    name = re.split(r"\s[|–—-]\s", rec.get("source") or "")[0].strip().lower()
    name = re.sub(r"^the\s+", "", name)
    if name in OUTLET_SITES:
        return OUTLET_SITES[name]
    return name if re.fullmatch(r"[a-z0-9\-]+(\.[a-z0-9\-]+)+", name) else ""


def image_ext(data: bytes) -> str:
    """File extension for image bytes we are willing to serve, else ''. Skips tiny placeholder PNGs."""
    if not 150 <= len(data) <= 400_000:
        return ""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png" if int.from_bytes(data[16:20], "big") >= 32 else ""
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:4] == b"\x00\x00\x01\x00":
        return "ico"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] == b"GIF8":
        return "gif"
    return ""


def get_bytes(url: str, timeout: float = 6) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "image/*,text/html;q=0.9,*/*;q=0.5"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(600_000)


def fetch_icon(domain: str):
    """(bytes, ext) for an outlet's icon: its own home-screen icon when it offers one, else a favicon service."""
    urls = [f"https://www.{domain}/apple-touch-icon.png", f"https://{domain}/apple-touch-icon.png"]
    try:  # the icon the site itself advertises
        html = get_bytes(f"https://{domain}/").decode("utf-8", "replace")
        for tag in re.findall(r"<link\b[^>]*>", html, re.I):
            if re.search(r"""rel=["'][^"']*apple-touch-icon""", tag, re.I):
                href = re.search(r"""href=["']([^"']+)""", tag, re.I)
                if href:
                    urls.insert(0, urllib.parse.urljoin(f"https://{domain}/", href.group(1).replace("&amp;", "&")))
                    break
    except Exception:
        pass
    urls += [f"https://www.google.com/s2/favicons?sz=128&domain={domain}",
             f"https://icons.duckduckgo.com/ip3/{domain}.ico", f"https://{domain}/favicon.ico"]
    for u in urls:
        try:
            data = get_bytes(u)
        except Exception:
            continue
        ext = image_ext(data)
        if ext:
            return data, ext
    return None, ""


def update_icons(news, path, now, fetcher=None):
    """Save an icon for each outlet on the page (a few new ones per run) and point each story at its file."""
    fetcher = fetcher or fetch_icon
    folder = os.path.join(os.path.dirname(path) or ".", "icons")
    os.makedirs(folder, exist_ok=True)
    idx_path = os.path.join(folder, "index.json")
    try:
        with open(idx_path) as f:
            idx = json.load(f)
    except Exception:
        idx = {}
    count = {}
    for rec in news["items"]:
        dom = outlet_site(rec)
        if dom and re.fullmatch(r"[a-z0-9.\-]{3,80}", dom) and ".." not in dom:
            rec["domain"] = dom
            count[dom] = count.get(dom, 0) + 1
    got = 0
    deadline = time.time() + 60
    for dom in sorted(count, key=lambda d: -count[d]):
        e = idx.get(dom)
        if e and (e.get("f") and os.path.exists(os.path.join(folder, e["f"])) or
                  not e.get("f") and now - e.get("at", 0) < ICON_RETRY_DAYS * 86400):
            continue
        if got >= ICONS_PER_RUN or time.time() > deadline:
            break
        got += 1
        data, ext = fetcher(dom)
        name = f"{dom}.{ext}" if data else ""
        if data:
            with open(os.path.join(folder, name), "wb") as f:
                f.write(data)
        idx[dom] = {"f": name, "at": int(now)}
    for rec in news["items"]:
        f = (idx.get(rec.get("domain") or "") or {}).get("f")
        if f and os.path.exists(os.path.join(folder, f)):
            rec["icon"] = "icons/" + f
        else:
            rec.pop("icon", None)
    with open(idx_path, "w") as f:
        json.dump(idx, f, separators=(",", ":"), sort_keys=True)
    if got:
        print(f"outlet icons: tried {got}, {sum(1 for e in idx.values() if e.get('f'))} saved in total")


def safe_icons(news, path, now):
    try:
        update_icons(news, path, now)
    except Exception as e:  # icons are a nicety; never let them stop the news
        print(f"outlet icons failed: {e}", file=sys.stderr)


def trusted_record(rec) -> bool:
    """Is this saved story from an outlet good enough to stand behind a lasting fact?"""
    src = rec.get("source") or ""
    return outlet_tier({"source": src}) >= 2 or bool(re.match(r"(?i)(parliament|government of canada|bank of canada)", src))


def safe_keystones(news, path, now):
    if os.environ.get("FEED_FILE") or os.environ.get("DRY_RUN") == "1":
        return
    try:
        import keystones
        keystones.update(news, path, now, trusted=trusted_record)
    except Exception as e:  # never let the daily summaries stop the news
        print(f"keystones failed: {e}", file=sys.stderr)


def safe_parliament(path, now):
    if os.environ.get("FEED_FILE") or os.environ.get("DRY_RUN") == "1":
        return
    try:
        import parliament
        parliament.update(path, now, sys.modules[__name__])
    except Exception as e:  # the Parliament view is extra; never let it stop the news
        print(f"parliament failed: {e}", file=sys.stderr)


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
    safe_keystones(news, path, now)
    safe_icons(news, path, now)
    safe_parliament(path, now)
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
    safe_keystones(news, path, now)
    safe_icons(news, path, now)
    safe_parliament(path, now)
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


def parse_fedreg(data: bytes, name: str):
    """Items from the U.S. Federal Register's JSON (published documents, or those filed for publication)."""
    for d in json.loads(data).get("results", []):
        title, link = (d.get("title") or "").strip(), d.get("html_url") or ""
        when = d.get("publication_date") or d.get("filed_at") or ""
        if not title or not link:
            continue
        agencies = ", ".join(a.get("name") or a.get("raw_name") or "" for a in d.get("agencies") or [])[:120]
        yield {"title": title, "link": link, "desc": re.sub(r"\s+", " ", (d.get("abstract") or agencies or ""))[:400],
               "source": name, "source_url": "https://www.federalregister.gov", "ts": parse_time(when[:10]) if when else time.time(),
               "guid": d.get("document_number") or link}


GAZETTE_SKIP = re.compile(r"(?i)^(table of contents|html|pdf|previous|next|top of page|date modified|search|skip to|"
                          r"canada gazette|government notices|parliament|commissions|miscellaneous notices|proposed regulations|"
                          r"supplements?|index|.{0,11})$")


def gazette_entries(page: str, base: str):
    """(title, link) for each order, regulation or notice in one issue of the Canada Gazette."""
    out, seen = [], set()
    for href, text in re.findall(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S):
        title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()
        link = urllib.parse.urljoin(base, href)
        if "/rp-pr/" not in link or not link.endswith(".html") or link in seen or GAZETTE_SKIP.match(title) or len(title) < 12:
            continue
        seen.add(link)
        out.append((title[:240], link))
    return out


GAZETTE_SECTIONS = re.compile(r"(?i)^(government notices|proposed regulations|commissions)$")


def gazette_notices(page: str, base: str):
    """(title, link) for each notice inside a Part I section page: department, Act, then the notice's own title."""
    out, cur = [], None

    def flush():
        if cur and (cur["notice"] or cur["act"]):
            what = cur["notice"] or cur["act"]
            out.append((f"{what} ({cur['dept'].title()})"[:240], f"{base}#{cur['id']}"))
    for tag, attrs, inner in re.findall(r"<(h[2-4])\b([^>]*)>(.*?)</\1>", page, re.S):
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", inner))).strip()
        anchor = re.search(r'id="([^"]+)"', attrs)
        if tag == "h2":
            flush()
            cur = {"dept": text, "id": anchor.group(1), "act": "", "notice": ""} if anchor and text else None
        elif cur and tag == "h3" and not cur["act"]:
            cur["act"] = text
        elif cur and tag == "h4" and not cur["notice"]:
            cur["notice"] = text
    flush()
    return out


def read_gazette(src, data):
    """The two newest issues, opened up into their individual entries. Checked about once an hour."""
    if int(time.time() // 300) % 12 and not os.environ.get("GAZETTE_NOW"):
        return []
    items = []
    for issue in list(parse(data, src.get("name", "")))[:2]:
        try:
            page = fetch(issue["link"]).decode("utf-8", "replace")
        except Exception:
            continue
        found = gazette_entries(page, issue["link"])
        # Part I groups its notices on a few section pages; open those too.
        for href, text in re.findall(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S):
            if GAZETTE_SECTIONS.match(re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()):
                section = urllib.parse.urljoin(issue["link"], href)
                try:
                    found += gazette_notices(fetch(section).decode("utf-8", "replace"), section)
                except Exception:
                    pass
        for title, link in found:
            items.append({"title": title, "link": link, "desc": "", "source": src["name"], "source_url": "https://www.gazette.gc.ca",
                          "ts": issue["ts"], "guid": link})
    return items


def read_source(src, feed_override):
    data = open(feed_override, "rb").read() if feed_override else fetch(src["url"])
    if src.get("kind") == "fedreg" and not feed_override:
        items = list(parse_fedreg(data, src.get("name", "")))
    elif src.get("kind") == "gazette" and not feed_override:
        items = read_gazette(src, data)
    else:
        items = list(parse(data, src.get("name", "")))
    if src.get("keep"):
        items = [it for it in items if re.search(src["keep"], f"{it['title']} {it['desc']}")]
    if src.get("skip"):
        items = [it for it in items if not re.search(src["skip"], it["title"])]
    for it in items:
        if src.get("lang"):
            it["lang"] = src["lang"]
        if src.get("primary"):   # an organisation's own statement: reliable for what that organisation says
            it["official"] = True
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


def check_urls(urls):
    """Diagnostic: one line per address saying whether it answers and whether it is a feed."""
    from concurrent.futures import ThreadPoolExecutor

    def one(u):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": BROWSER_UA, "Accept": "*/*", "Accept-Language": "en-CA,en;q=0.9"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read(600000).decode("utf-8", "replace")
                n = len(re.findall(r"<item[\s>]|<entry[\s>]", body))
                kind = "feed" if n else ("json" if body.lstrip()[:1] in "{[" else "html" if "<html" in body[:3000].lower() else "other")
                titles = [html.unescape(re.sub(r"<!\[CDATA\[|\]\]>|<[^>]+>", "", t)).strip()[:60]
                          for t in re.findall(r"<title[^>]*>(.*?)</title>", body, re.S)[1:3]]
                dates = re.findall(r"<(?:pubDate|updated|published)>([^<]{10,31})", body)[:1]
                return f"CHECK {resp.status} {kind:5} {n:3} items | {u[:95]} | {dates} {titles if kind == 'feed' else body[:70].split()[:8]}"
        except Exception as e:
            return f"CHECK FAIL       | {u[:95]} | {type(e).__name__}: {str(e)[:50]}"
    with ThreadPoolExecutor(max_workers=10) as pool:
        for line in pool.map(one, urls):
            print(line)
    return 0


def probe(urls):
    """Diagnostic: fetch each URL from the runner and print what comes back."""
    if urls.strip() == "gazette-debug":
        issue = list(parse(fetch("https://www.gazette.gc.ca/rss/p1-eng.xml")))[0]
        page = fetch(issue["link"]).decode("utf-8", "replace")
        links = re.findall(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S)
        rows = [(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t)).strip()[:70], urllib.parse.urljoin(issue["link"], h)) for h, t in links]
        rows = [r for r in rows if "/rp-pr/" in r[1]]
        print("GZ issue", issue["link"], len(rows))
        for t, l in rows[:30]:
            print("GZ  ", t, "|", l[-60:])
        sub = next((l for t, l in rows if re.search(r"(?i)government notices|proposed regulations", t)), None)
        if sub:
            sp = fetch(sub).decode("utf-8", "replace")
            heads = re.findall(r"<(h[2-4])\b([^>]*)>(.*?)</\1>", sp, re.S)
            print("GZ sub", sub[-60:], len(heads))
            for tag, attrs, t in heads[:25]:
                print("GZ   ", tag, re.findall(r'id="([^"]+)"', attrs)[:1], re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t)).strip()[:90])
        st = fetch("https://www150.statcan.gc.ca/n1/rss/dai-quo/0-eng.atom").decode("utf-8", "replace")
        print("ST", re.sub(r"\s+", " ", st[st.find("<entry"):st.find("<entry") + 700]))
        return 0
    if urls.strip().startswith("sources "):
        # Read the named sources for real and say what each returned.
        os.environ["GAZETTE_NOW"] = "1"
        want = urls.split()[1:]
        for src in all_sources():
            if not any(w in src["id"] for w in want):
                continue
            try:
                items = read_source(src, None)
                rel = [it for it in items if classify(it)]
                eg = (rel or items or [{"title": ""}])[0]["title"][:80]
                print(f"SRC ok   {src['id']:<28} {len(items):>3} items, {len(rel):>2} relevant | {eg!r}")
            except Exception as e:
                print(f"SRC FAIL {src['id']:<28} {type(e).__name__}: {str(e)[:70]}")
        return 0
    if urls.strip().startswith("check "):
        return check_urls(urls.split()[1:])
    if urls.strip() == "committee-inbox":
        import committee_mail
        return committee_mail.debug_inbox()
    if urls.strip().startswith("committee-doc "):
        import committee_mail
        return committee_mail.debug(sys.modules[__name__], urls.split()[1])
    if urls.strip() == "keystones":
        import keystones
        return keystones.debug(sys.modules[__name__])
    if urls.strip() == "parliament":
        import parliament
        return parliament.debug(sys.modules[__name__])
    for u in urls.split():
        try:
            browser = u.startswith("b:")  # "b:https://…" = ask the way a browser would
            u = u[2:] if browser else u
            req = urllib.request.Request(u, headers={"User-Agent": BROWSER_UA if browser else UA, "Accept": "*/*",
                                                     "Accept-Language": "en-CA,en;q=0.9"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                body = resp.read(2000000)
                text = body.decode("utf-8", "replace")
                links = sorted(set(re.findall(r'(?:href|src)="([^"]*(?:rss|feed|atom|xml)[^"]*)"', text, re.I)))[:15]
                print(f"PROBE {resp.status} {resp.headers.get('Content-Type')} len={len(body)} {u}")
                print("   head:", re.sub(r"\s+", " ", text[:int(os.environ.get("PROBE_CHARS") or 500)]))
                print("   keys:", sorted(set(re.findall(r'"(\w*(?:DateTime|Reading|Assent|Committee)\w*)":', text)))[:80])
                if "<html" in text[:2000].lower():
                    plain = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " | ", re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text))))
                    plain = re.sub(r"( \| )+", " | ", plain)
                    at = max(plain.lower().find("notice of meeting", 600), 0)
                    print("   text:", plain[at:at + 3500])
                mark = os.environ.get("PROBE_FIND", "This enactment")
                if mark in text:
                    cut = text[text.find(mark) - 200: text.find(mark) + 1500]
                    print("   found:", re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cut))))
                docs = re.findall(r'href="([^"]*DocumentViewer[^"]*)"[^>]*>\s*([^<]{0,90})', text)
                if docs:
                    print(f"   document links ({len(docs)}):", docs[:10])
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


GO_KEEP_DAYS = 3


def save_go_links(news_path, items, now):
    """go.json: story id -> article address for recent alerts. The small page a notification opens
    (go.html) reads this instead of the whole news file, so it works quickly on a slow connection."""
    path = os.path.join(os.path.dirname(news_path) or ".", "go.json")
    try:
        with open(path) as f:
            links = json.load(f)
    except Exception:
        links = {}
    for it in items:
        if re.match(r"https?://", it.get("link") or ""):
            links[item_id(it)] = [it["link"], int(now)]
    links = {k: v for k, v in links.items() if isinstance(v, list) and now - v[1] < GO_KEEP_DAYS * 86400}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(links, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)


# ------------------------------- health check --------------------------------
AI_FAILS_BEFORE_ALERT = 3      # runs in a row (about 15 minutes)
SOURCE_DOWN_HOURS = 24
REPEAT_ALERT_HOURS = 12


def health_check(state, now, quiet, local, ai_wanted, ai_ok, failed, send_alert):
    """Notice slow-burning problems and say so once: Claude not rating stories, or a source dead for a day.
    (A separate workflow, health.yml, watches for the runs themselves stopping.)"""
    h = state.setdefault("health", {})
    if ai_wanted:
        if ai_ok:
            if h.get("ai_alerted"):
                send_alert("News alerts: Claude is rating stories again", "Back to normal.", ["white_check_mark"])
            h["ai_fails"], h["ai_alerted"] = 0, 0
        else:
            h["ai_fails"] = h.get("ai_fails", 0) + 1
            h["ai_error"] = AI_LAST_ERROR
            if h["ai_fails"] >= AI_FAILS_BEFORE_ALERT and not quiet \
                    and now - h.get("ai_alerted", 0) > REPEAT_ALERT_HOURS * 3600:
                send_alert("News alerts: Claude isn't rating stories",
                           f"Stories are being rated by keyword rules instead, so ratings and summaries are rougher. "
                           f"Reason: {AI_LAST_ERROR or 'unknown'}. If it mentions credit or the API key, check "
                           f"console.anthropic.com.", ["warning"])
                h["ai_alerted"] = now
    down = h.setdefault("down", {})
    for sid in list(down):
        if sid not in failed:
            del down[sid]
    for sid in failed:
        down.setdefault(sid, now)
    today = local.strftime("%Y-%m-%d")
    dead = sorted(sid for sid, since in down.items() if now - since > SOURCE_DOWN_HOURS * 3600)
    if dead and not quiet and local.hour >= 9 and h.get("src_day") != today:
        h["src_day"] = today
        send_alert(f"News alerts: {len(dead)} source{'s' if len(dead) > 1 else ''} down for over a day",
                   "Not answering: " + ", ".join(d.split(":", 1)[-1] for d in dead[:8])
                   + (f" and {len(dead) - 8} more" if len(dead) > 8 else "")
                   + ". Other sources still cover the news; these may need replacing.", ["warning"])


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

    fresh, ok, total, new_seeds, failed_sources = [], 0, 0, [], []
    results = list(read_all(feed_override))
    committee_items = []
    if not feed_override:   # items from the House of Commons committee e-mail subscription
        try:
            import committee_mail
            committee_items = committee_mail.collect(
                state, now, sys.modules[__name__],
                lambda title, msg, link: send(server, topic, title, msg, click=link, priority=4, tags=["incoming_envelope"]))
            committee_mail.record(os.environ.get("NEWS_FILE", "").strip(), committee_items, now, sys.modules[__name__])
        except Exception as e:
            print(f"committee inbox failed: {e}", file=sys.stderr)
        if committee_items:
            results.append(({"id": "mail:committees"}, committee_items, None))
    for src, items, err in results:
        total += 1
        if err:
            print(f"[{src['id']}] failed: {err}", file=sys.stderr)
            failed_sources.append(src["id"])
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
    if news_path:   # long-running stories that have gone quiet keep their thread
        try:
            import keystones
            stories += keystones.on_file(news_path, now, have={g["tid"] for g in stories})
        except Exception as e:
            print(f"keystones on file: {e}", file=sys.stderr)
    ai_ok = bool(cands) and ai_score(cands[:40], already, stories)
    if ai_ok:
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
    try:
        health_check(state, now, quiet, local, bool(cands) and bool(os.environ.get("ANTHROPIC_API_KEY")), ai_ok,
                     failed_sources, lambda title, msg, tags: send(server, topic, title, msg, priority=3, tags=tags))
    except Exception as e:  # the health check must never stop the news
        print(f"health check: {e}", file=sys.stderr)
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
             click=PAGE_URL + "go.html?go=" + item_id(it) if news_path else it["link"],
             priority=max(1, min(5, it["level"])), tags=[ASSOCS[assoc]["emoji"], tag])
        print(f"sent L{it['level']} [{it['topic']['key']}]:", it["title"], "|", it["why"])
    if news_path and to_send:
        try:
            save_go_links(news_path, to_send[:MAX_PER_RUN], now)
        except Exception as e:
            print(f"go links: {e}", file=sys.stderr)
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
        parts = [f"{a}: {brief[a]}" for a in ASSOC_ORDER if brief.get(a) and now - brief.get("at", 0) < 3 * 3600]
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
