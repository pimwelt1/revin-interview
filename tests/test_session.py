from conftest import call, say, talk
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voice_agent.prompts import GREETING
from voice_agent.session import TOOL_INTERRUPTED


async def transcript(session):
    return (await session.graph.aget_state(session.config)).values["messages"]


async def test_reply_streams_agent_text_and_greeting_is_in_transcript(make_session):
    session, _ = make_session(say("Sorry to hear that. What's going on with it?"))

    assert await talk(session, "My AC is broken") == "Sorry to hear that. What's going on with it?"

    messages = await transcript(session)
    assert [type(m) for m in messages] == [AIMessage, HumanMessage, AIMessage]
    assert messages[0].content == GREETING


async def test_end_call_finishes_the_session_without_another_model_call(make_session):
    session, model = make_session(call("end_call"), say("should never be generated"))

    assert await talk(session, "That's all, thanks") == ""

    assert session.finished and not session.failed
    assert len(model.received) == 1


async def test_interrupted_reply_keeps_only_what_the_caller_heard(make_session):
    session, _ = make_session(say("Our first window is Tuesday at 10 AM and the second is Wednesday."), say("Okay."))
    turn = session.start_turn("When can you come?")
    async for _ in session.reply(turn):
        pass
    session.interrupt("Our first window is Tuesday at 10 AM")

    await talk(session, "Tuesday works")

    messages = await transcript(session)
    assert messages[2].content == "Our first window is Tuesday at 10 AM [interrupted]"
    assert messages[3].content == "Tuesday works"


async def test_tool_calls_left_open_by_a_cancelled_run_are_closed(make_session):
    session, _ = make_session(say("Let me check again."))
    await session.graph.aupdate_state(
        session.config,
        {"messages": [HumanMessage("Book it"), call("find_available_slots")]},
        as_node="agent",
    )
    session.phone_notes = []

    await talk(session, "Hello?")

    messages = await transcript(session)
    assert isinstance(messages[2], ToolMessage) and messages[2].content == TOOL_INTERRUPTED
    assert messages[3].content == "Hello?"


async def test_session_follows_the_language_chooser_and_sends_it_to_the_agent(make_session):
    session, model = make_session(say("Claro."))

    await talk(session, "Hola, necesito ayuda con mi calefacción", language="es-MX")

    assert session.language == "es"
    assert "Speak Spanish" in model.received[-1][0].content


async def test_model_failure_marks_the_session_failed(make_session, monkeypatch):
    session, model = make_session(say("unused"))

    def broken(*args, **kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr(type(model), "_generate", broken)

    assert await talk(session, "Hello") == ""
    assert session.failed


def test_short_turns_and_other_languages_do_not_count(make_session):
    session, _ = make_session()
    assert session.choose_language("hein", "fr-FR") == "en"
    assert session.choose_language("je voudrais un technicien", "fr-FR") == "en"
    assert session.choose_language("my heater is broken", "") == "en"
    assert session.language_votes == []


def test_short_spanish_turn_means_spanish_until_a_turn_counts(make_session):
    session, _ = make_session()
    assert session.choose_language("español", "es-MX") == "es"
    assert session.language_votes == []  # didn't count
    assert session.choose_language("my heater is broken", "en-US") == "en"


def test_language_follows_the_latest_turn_until_two_in_a_row_agree(make_session):
    session, _ = make_session()
    assert session.choose_language("my heater is broken", "en-US") == "en"
    assert session.choose_language("necesito ayuda por favor", "es-MX") == "es"
    assert session.choose_language("no funciona desde ayer", "es-MX") == "es"
    assert session.choose_language("my address is twelve Oak Street", "en-US") == "es"  # one English sentence
    assert session.choose_language("I prefer English, thank you", "en-US") == "en"  # two in a row
