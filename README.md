# SummitAir voice agent

A phone assistant for an HVAC company. It answers when the team can't, and books a 15-minute callback from a technician. It never books a visit itself.

- Books, reschedules and cancels technician callbacks against real Google Calendars.
- Validates the caller's details before booking; a street address must resolve through geocoding.
- Emails the team for urgent issues instead of booking. For real emergencies (ex: gas), it says one go to safety line and hangs up.
- Speaks English or Spanish, following the caller.
- Serves Manhattan and Brooklyn (`COUNTIES`); addresses outside a technician's radius are declined.

## Run it

**Tests — no credentials, no network.** Fastest way to see the behaviour:

```bash
pip install -r requirements.txt
pytest # 86 tests
```

**Talk to it by typing.** Same `Session` a real call uses, so this exercises the whole agent:

```bash
cp .env.example .env        # fill in OpenAI + Composio, see Configuration
PYTHONPATH=src python -m voice_agent.chat
```

**On the phone**, point a Twilio number's voice webhook at `POST /voice` on a public HTTPS host:

```bash
PYTHONPATH=src uvicorn voice_agent.main:app --port 8080
```

`Dockerfile` and `fly.toml` deploy it as one machine with a volume.

## How it works

```
caller utterance → [details] → [agent] ⇄ [tools] → spoken reply
```

Every caller turn runs `details` first: a model call with a strict JSON schema extracts only what changed, then [`details.py`](src/voice_agent/agent/details.py) validates it. The conversational agent then speaks, seeing which details are accepted, which are pending, and what to ask for next. 

Seven tools: `find_available_slots`, `book_service_call`, `find_my_service_calls`, `reschedule_service_call`, `cancel_service_call`, `report_urgent_issue`, `end_call`.

## Key Design decisions

- **Validate before speaking, not after.** Booking on a misheard address or a 9-digit phone number is the expensive failure, so the graph validates each turn before the agent talks back.
- **Tools are the source of truth.** The agent may only claim a booking, change or alert if a tool returned success. Guards enforce it in code, not just in the prompt: a slot must come from the latest search, a change must name a call from the latest lookup, and not in the same turn it was listed.
- **CSV storage with an in-process lock.** Storage is kept as simple as possible for this demo. Single process by design, atomic writes, dozens of rows. Every check-then-write sequence holds the lock, so two concurrent calls can't book the same slot.
- **Voice-first.** Replies stream token by token; interruptions record what the caller actually heard, so the transcript doesn't claim it said something that was cut off.

## Configuration

All settings live in [`.env`](.env.example) — credentials, business hours, service radius, service area.
Technicians are listed in `data/technicians.csv` (`name,calendar_id,address`).

Composio needs Google Calendar, Gmail and Google Maps connected for `COMPOSIO_USER_ID`.
