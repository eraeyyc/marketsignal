another thing i was considering: we may also be able to just look for particular TYPES of aircraft, not necessarily just the specific tail numbers. for example, 1\. Diplomatic jet clustering

When you suddenly see Gulfstream / Global / Falcon traffic clustering around places like:

* Doha  
* Muscat  
* Ankara  
* Amman  
* Riyadh

that’s usually not tourism.

Back-channel negotiations almost always happen in neutral intermediaries like Oman or Qatar.

What you want to detect is baseline deviation.

2. Military repositioning

This is where ADS-B gets interesting.

Signals that historically precede escalation:

* C-17 / C-5 surges  
* KC-135 / KC-46 tankers staging  
* ISR aircraft concentration  
* carrier logistics flights

Military logistics aircraft are louder than fighters.

Everyone watches fighters because they look dramatic.

But fighters deploy after logistics is staged.

The aircraft that matter more are:

Strategic lift

* C-17  
* C-5  
* Il-76  
* Y-20

Tankers

* KC-135

* KC-46

* A330 MRTT ISR / command aircraft

* E-3 AWACS

* E-8 JSTARS

* RC-135

* Global Hawk

When these start clustering around a region, it means planning is already underway.

Tankers especially.

If tankers surge, fighters are coming.

There’s a another signal that intelligence analysts quietly rely on: logistics traffic.

Things like:

* fuel shipments  
* satellite imagery of base activity  
* shipping movements  
* military cargo flights Wars run on logistics.

Logistics spikes often appear weeks before operations.

The sequence is actually fairly consistent historically:

Week 3-4 before operations:  
→ Strategic lift surges (C-17, C-5) — moving equipment and personnel  
→ Tanker pre-positioning begins  
→ ISR aircraft concentration (they need to map the environment)

Week 1-2 before operations:  
→ Tanker staging intensifies  
→ Command and control aircraft appear (AWACS, JSTARS)  
→ Logistics flights to forward bases spike

Days before:  
→ Fighters deploy to forward positions  
→ Everything goes dark or changes callsigns  
→ NOTAMs start appearing  
→ Commercial routes start pulling  
By the time fighters are visible, you're already late. The tankers were the signal.

**How aircraft type detection actually works in ADS-B**

ADS-B transponders broadcast an ICAO24 hex code which maps to a registration. But they also broadcast aircraft type codes in many cases. ADS-B Exchange and OpenSky both carry type information in their data.

The practical workflow:

\# Instead of watching specific tail numbers  
\# Watch for TYPE codes appearing in geographic bounding boxes

military\_types \= {  
    'strategic\_lift': \['C17', 'C5M', 'IL76', 'Y20'\],  
    'tankers': \['KC135', 'KC46', 'A332'\],  \# A332 \= A330 MRTT  
    'isr\_command': \['E3CF', 'RC135', 'E8', 'RQ4'\],  
    'awacs': \['E3TF', 'E3CF'\]  
}

watched\_regions \= {  
    'persian\_gulf': {'lat': (22, 30), 'lon': (48, 60)},  
    'eastern\_med': {'lat': (30, 38), 'lon': (28, 38)},  
    'horn\_of\_africa': {'lat': (8, 16), 'lon': (40, 52)}  
}

\# Flag: unusual concentration of type X in region Y  
\# compared to 30-day rolling baseline  
\`\`\`

\---

\*\*The diplomatic clustering layer\*\*

The Gulfstream/Global/Falcon approach is clever because it sidesteps the tail number problem entirely for the diplomatic signal. You don't need to know \*whose\* G650 it is — you need to know that six large-cabin bizjets just appeared at Muscat that weren't there yesterday.

The baseline question becomes: what's normal bizjet traffic at Muscat on a Tuesday? Anything significantly above that baseline, especially with multiple aircraft arriving within a short window from different origin points, is a clustering signal worth flagging.

ADS-B Exchange actually has aircraft category codes that distinguish between:  
\- Large commercial  
\- Business jets (which is what Gulfstreams etc. register as)  
\- Military  
\- Special purpose

So you can filter for bizjet category, apply geographic bounding box around Muscat, and count against baseline. That's genuinely buildable without any tail number database at all.

\---

\*\*The OPSGROUP page we just read is relevant here\*\*

They specifically noted Muscat becoming a staging point right now — bizjet and airline traffic clustering there because it sits just outside the most restricted Gulf airspace. That's the exact signal. It's happening in real time and it's visible in ADS-B data.

\---

\*\*What this means for the build\*\*

You now have two parallel detection approaches that complement each other:  
\`\`\`  
Approach 1: Tail number watchlist (Jetnet)  
→ High precision, known actors  
→ Requires maintained database  
→ Misses unknown or newly relevant aircraft

Approach 2: Aircraft type \+ geographic clustering  
→ Lower precision but much broader coverage  
→ Self-maintaining — types don't change  
→ Catches patterns even without knowing specific actors  
→ Particularly powerful for logistics/tanker detection  
\`\`\`

Running both simultaneously and cross-referencing is stronger than either alone. A known diplomatic tail number appearing at Muscat is a strong signal. Six unidentified bizjets clustering at Muscat is a moderate signal. A known tail number appearing alongside an unusual bizjet cluster at Muscat is a very strong signal.

\---

\*\*One more thing worth flagging\*\*

The ISR aircraft concentration signal you mentioned — RC-135s, Global Hawks, E-3 AWACS — these are particularly interesting because their presence means someone is already collecting intelligence on a target. ISR doesn't deploy speculatively. If RC-135s are orbiting a region, there's already an active intelligence requirement driving that. That's a late-stage pre-operation signal that's more precise than tanker positioning.

The hierarchy from earliest to latest pre-operation signal is roughly:  
\`\`\`  
Earliest (weeks out):  
Strategic lift surge → logistics pre-positioning

Middle (1-2 weeks):  
Tanker staging → range extension being established

Later (days):  
ISR concentration → active target intelligence collection  
AWACS positioning → air battle management being set up  
Commercial route pulls → risk intelligence saying something imminent  
NOTAM restricted airspace → operational preparation

Latest (hours):  
Fighters forward deploy  
Everything goes dark

---

The naive version of convergence scoring assumes signals fire simultaneously. But the sequence we just mapped out plays out over weeks. A tanker surge in week three and a NOTAM restricted airspace filing in week one are part of the same escalation signature — the system needs to recognize that even though they're temporarliy separated.

**The concept: Decaying Signal Memory**

Each signal fires and gets added to a running score that decays over time rather than resetting. Think of it like a bucket with a slow leak — each signal adds water, time drains it. If enough signals fire within a relevant window, the bucket overflows and triggers an alert.

signal\_memory \= {  
    'tanker\_surge': {  
        'score': 0.8,  
        'timestamp': '2026-02-10',  
        'decay\_rate': 0.05  \# loses 5% per day  
    },  
    'strategic\_lift\_surge': {  
        'score': 0.6,  
        'timestamp': '2026-02-14',  
        'decay\_rate': 0.05  
    },  
    'notam\_restriction': {  
        'score': 0.9,  
        'timestamp': '2026-02-26',  
        'decay\_rate': 0.15  \# decays faster \- more time sensitive  
    }  
}

def current\_score(signal, current\_date):  
    days\_elapsed \= (current\_date \- signal\['timestamp'\]).days  
    return signal\['score'\] \* (1 \- signal\['decay\_rate'\]) \*\* days\_elapsed

total\_score \= sum(current\_score(s, today) for s in signal\_memory.values())  
\`\`\`

\---

\*\*Different signals should decay at different rates\*\*

This is the nuanced part. Not all signals have the same shelf life:  
\`\`\`  
Slow decay (weeks):  
→ Tanker pre-positioning  
→ Strategic lift surge  
→ ISR concentration buildup  
These indicate sustained planning — still relevant weeks later

Medium decay (days):  
→ Diplomatic bizjet clustering  
→ Commercial route suspensions  
→ Semantic drift acceleration  
These are meaningful for days but stale within a week or two

Fast decay (hours):  
→ NOTAM restricted airspace filing  
→ Aircraft going dark  
→ AWACS forward positioning  
These are immediate precursors — highly time sensitive  
\`\`\`

The system weights recent fast-decay signals very heavily while older slow-decay signals provide background context.

\---

\*\*The escalation sequence as a scoring template\*\*

Remember the sequence we mapped:  
\`\`\`  
Weeks 3-4: Strategic lift \+ tanker surge  
Weeks 1-2: Tanker staging intensifies \+ ISR concentration    
Days before: AWACS \+ commercial pulls \+ NOTAMs  
Hours before: Dark aircraft \+ fighter deployment

---

You can encode that sequence as an expected pattern. The system then scores not just signal presence but **sequential coherence** — are signals firing in the right order?

​​escalation\_sequence \= \[  
    {'signal': 'strategic\_lift\_surge', 'expected\_lead\_days': 21},  
    {'signal': 'tanker\_surge', 'expected\_lead\_days': 14},  
    {'signal': 'isr\_concentration', 'expected\_lead\_days': 7},  
    {'signal': 'notam\_restriction', 'expected\_lead\_days': 2},  
    {'signal': 'aircraft\_dark', 'expected\_lead\_days': 0.5}  
\]  
\`\`\`

A pattern that matches the expected sequence scores higher than the same signals firing in random order. Because random order might be noise. Sequential coherence is what an actual operation looks like.

\---

\*\*The time window problem\*\*

You also need to decide: how far back does the system look? 

Too short — misses early signals that are still relevant  
Too long — everything becomes noise because unrelated signals accumulate

The practical answer is probably a \*\*dynamic window\*\* based on signal type:  
\`\`\`  
Operational window: 30 days  
→ Any signals within 30 days contribute to score  
→ Weighted by recency and decay rate

Alert threshold review: daily  
→ Score recalculated every 24 hours  
→ Trend matters as much as absolute score  
→ Score accelerating upward is itself a signal  
\`\`\`

That last point is important — a score moving from 0.2 to 0.4 to 0.7 over three days is more alarming than a static score of 0.7.

\---

\*\*The de-escalation mirror\*\*

The same time-lag logic works in reverse for detecting ceasefire signals. De-escalation signals that stack across a window:

\- Diplomatic bizjets appearing at neutral airports  
\- Semantic drift toward negotiation vocabulary accelerating  
\- Tanker repositioning away from forward positions  
\- Commercial airlines quietly reopening routes  
\- NOTAM restrictions being lifted

Those stack the same way. A ceasefire market on Polymarket might actually be more tradeable right now than an escalation market — because escalation already happened, but de-escalation timing is genuinely uncertain and the signals are readable.

\---

\*\*What to add to the Claude Code prompt\*\*

When you go back to build, add something like:  
\`\`\`  
Implement time-lag scoring with the following properties:

1\. Each signal fires with an initial score and a signal-type   
   specific decay rate

2\. Total convergence score is the sum of all decayed signal   
   scores within a 30-day rolling window

3\. Score trend (acceleration) is calculated alongside   
   absolute score — a rapidly rising score triggers   
   earlier alerts than a static high score

4\. Sequential coherence bonus: signals firing in the   
   expected escalation sequence order receive a   
   multiplier on their contribution

5\. Separate scoring tracks for escalation signatures   
   and de-escalation signatures running simultaneously

---

**Here's where the full architecture stands now**

It's evolved quite a bit from this morning. The current design:

Data Sources:

→ NOTAM feeds (aviationweather.gov aggregator)

→ ADS-B Exchange (real-time)

→ OpenSky Network (historical baseline)

→ Aircraft type clustering (no tail number needed)

→ Jetnet watchlist (specific VIP tail numbers)

→ Facebook planespotter groups (human-curated anomaly layer)

→ GDELT (ground truth labeling)

→ State Dept / foreign ministry transcripts (semantic drift)

Signal Layers:

→ Layer 1: NOTAM anomaly detection

→ Layer 2: Aircraft type clustering by region

→ Layer 3: VIP tail number tracking

→ Layer 4: Going dark detection (live only)

→ Layer 5: Semantic drift velocity

→ Layer 6: Planespotter group anomaly feed

Scoring Engine:

→ Per-signal decay rates based on signal type

→ 30-day rolling window (pending your dad's input)

→ Sequential coherence multiplier

→ Trend acceleration scoring

→ Separate escalation \+ de-escalation tracks

Output:

→ Daily digest for human review

→ Convergence alerts above threshold

→ Relevant Polymarket markets surfaced

→ Human makes final judgment call

---

We have an existing architecture document for a geopolitical 

signal detection system. Please update the architecture to 

incorporate the following three additions. Don't rebuild 

from scratch — integrate these into the existing design.

\*\*Addition 1: Time-Lag Scoring with Decaying Signal Memory\*\*

Replace simple convergence scoring with a decaying signal 

memory system:

\- Each signal fires with an initial score (0-1) and a 

  signal-type specific decay rate

\- Total convergence score \= sum of all decayed signal 

  scores within a rolling time window (starting at 30 

  days, configurable)

\- Decay rates vary by signal type:

  \- Slow decay (weekly): strategic lift surge, 

    tanker pre-positioning, ISR concentration

  \- Medium decay (daily): diplomatic bizjet clustering, 

    commercial route suspensions, semantic drift 

    acceleration

  \- Fast decay (hourly): NOTAM restricted airspace, 

    aircraft going dark, AWACS forward positioning

\- Score TREND matters as much as absolute score — 

  a rapidly accelerating score triggers earlier alerts 

  than a static high score

\- Sequential coherence multiplier: signals firing in 

  the expected escalation sequence receive a score 

  bonus. Expected sequence is:

  1\. Strategic lift surge (weeks 3-4 before event)

  2\. Tanker staging (weeks 1-2)

  3\. ISR concentration (week 1\)

  4\. Commercial route suspensions (days before)

  5\. NOTAM restricted airspace (days before)

  6\. Aircraft going dark (hours before)

\- Run TWO parallel scoring tracks simultaneously: 

  escalation signature and de-escalation signature

\- All window lengths and decay rates must be 

  configurable constants, not hardcoded

\*\*Addition 2: Dual-Mode Aircraft Tracking\*\*

The aviation layer should track aircraft in two 

parallel modes simultaneously:

MODE A — Tail number watchlist:

\- Maintain a configurable watchlist of specific 

  ICAO24 transponder codes mapped to known VIP 

  and government aircraft (I have a list started in the project folder ‘vip aircraft.csv’ however it needs the ICAO24 numbers to be added. You’ll also find another file that can help with this titled ‘aircraft-database-complete.csv’)

\- Source: Jetnet database? (external, manually 

  maintained CSV for now)

\- Track: position, routing, clustering with other 

  watched aircraft, going dark events

\- Going dark detection requires persistent state — 

  log last known position and timestamp when a 

  watched aircraft stops transmitting

\- Flag proximity clustering: two or more watched 

  aircraft from different countries appearing at 

  the same airport within a 6-hour window

MODE B — Aircraft type clustering:

\- No tail number required

\- Monitor specific ICAO aircraft type codes within 

  geographic bounding boxes around watched regions

\- Type categories to monitor:

  Strategic lift: C17, C5M, IL76, Y20

  Tankers: KC135, KC46, A332 (A330 MRTT)

  ISR/Command: RC135, E3CF, E3TF, RQ4

  Diplomatic bizjets: GA8, F2TH, F900, GL5T, 

    GLEX, GLF4, GLF5, GLF6, C56X

    (Gulfstream, Global, Falcon category)

\- Watched regions with bounding boxes:

  Persian Gulf: lat 22-30, lon 48-60

  Eastern Med: lat 30-38, lon 28-38

  Horn of Africa: lat 8-16, lon 40-52

  Caucasus corridor: lat 38-44, lon 40-52

\- Baseline: rolling 30-day average count of each 

  type per region per time-of-day block

\- Flag: statistically significant deviation above 

  baseline (suggest 2+ standard deviations)

\- Diplomatic bizjet clustering specifically: flag 

  when 3+ bizjets arrive at a single watched airport 

  from different origin regions within 12 hours

Both modes feed into the same scoring engine.

A tail number hit scores higher than a type hit.

Both hitting simultaneously scores highest.

\*\*Addition 3: Planespotter Group Integration\*\*

Add a new data source layer: social media planespotter 

communities as a human-curated anomaly detection feed.

\- Primary sources: Facebook planespotter groups 

  (Middle East focused), Twitter/X aviation accounts

\- These communities notice and publish unusual 

  movements within minutes — essentially free 

  crowdsourced anomaly detection

\- Use AI to scrape and classify posts:

  \- Filter for posts mentioning watched aircraft 

    types or regions

  \- Score post urgency/significance based on 

    language ("unusual", "first time seen", 

    "not normal", specific military type mentions)

  \- Cross-reference with ADS-B data to validate

\- This layer acts as a fast-moving early warning 

  that can trigger closer inspection of ADS-B data

\- Lower confidence score than direct ADS-B 

  observation but faster — treat as an alert 

  to look harder, not a confirmed signal

\*\*What I need from you:\*\*

1\. Update the full architecture document incorporating 

   all three additions

2\. Flag any technical conflicts or gaps with the 

   existing design

3\. Update the staged build plan — where do these 

   additions fit in the sequence? Which are Stage 2, 

   which are later?

4\. For the aircraft type codes listed above — 

   validate these against actual ICAO type 

   designators and correct any errors

5\. Write the data schema for the signal memory 

   store — what does each signal event record 

   look like in the database?

6\. Flag any rate limiting or API constraint issues 

   with running dual-mode aircraft tracking 

   continuously against ADS-B Exchange and 

   OpenSky simultaneously

