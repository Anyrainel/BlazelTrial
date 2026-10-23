# **Take-home: A simple chatbot**

**Expected effort:** about 10–12 hours of focused work for someone fluent with Claude Code and Codex. **Return within** 5 days of receiving this.

## **What you'll need**

* Claude Code and/or OpenAI Codex (CLI or IDE).  
* An API key for any LLM provider.  
* A free Slack workspace you control.

## **Setup**

Build a chatbot that one user talks to on two surfaces:

|  | Main chat | Slack DM |
| ----- | ----- | ----- |
| **Shape** | The user creates new chat sessions and works in several in parallel. Each session starts with fresh context. | The user DMs your Slack app. It's one thread that never ends: hundreds of messages over months. |
| **Interface** | Your choice: CLI, HTTP, or a bare web page. No styling needed. | A real Slack app \- you can create one after signing up for free Slack org/account |

If you prefer, you may assume one user to avoid building authentication, etc. The person in the main chat and the person DMing your Slack app are the same; a hard-coded mapping is fine.

## **What to build**

A simple working chatbot. Preferably Python or Node.js/Typescript, and any LLM.

1. **One core entry point,** `send(user_id, thread_id, text) → reply`, used by both the main chat and the Slack app. `thread_id` is a session id or `slack-dm`. We must be able to call it ourselves, from the command line or over HTTP.  
2. **The agent must be able to:**  
   * **Read documents:** `fetch_doc(doc_id)` reads a file from a `docs/` folder. Each document is 20k–40k tokens. Generate a few, including one called `Q3-plan` that states a launch date.  
   * **Message someone:** `send_message(to, text)`. It doesn't really send anything; it logs the call.  
   * **Write a LinkedIn post:** at most 1,300 characters, at most 3 hashtags (all at the end), and no links.  
3.   
   Assume about 20 more capabilities like these will be added later, each with its own instructions.  
4. **Token budget:** every model call your system makes stays under **8,000 input tokens** as reported in the provider's usage field. Log the count for every call. The limit must be configurable.  
5. **State:** keep it in SQLite or files inside the repo directory.  
6. **A scenario script** that runs the scenarios below and prints pass/fail. It may call the core directly with `slack-dm` instead of going through Slack.

## **Scenarios**

1. **"I just told you."** In session A: *"My manager is Priya Raman."* Then, in session B (opened earlier) and in the Slack DM: *"Who's my manager?"* Both answer correctly.  
2. **Long thread.** Load about 500 turns of synthetic Slack DM history. Any method is fine, as long as it goes through the same code path as live messages. A question about a detail from around turn 20 is answered correctly.  
3. **Big document.** In the Slack DM: *"Summarize doc Q3-plan."* The doc is bigger than the budget. 100 turns later: *"What was the launch date in that plan?"* The answer is correct.  
4. **Changed mind.** *"Always use British spelling."* Later: *"Switch to American spelling."* New drafts use American spelling, and *"What did I say about spelling before?"* gets an answer that explains the change.  
5. **Stays local.** In session A: *"For this draft only, make it playful."* A new session B asks for a draft, and it is not playful.  
6. **Forget.** The user's home address came up early in the Slack DM, about 400 turns ago. *"Forget my address."* Then: *"Message my partner where I live."* The address appears neither in the reply nor in any `send_message` call, and it is gone from stored state.  
7. **LinkedIn post.** In the Slack DM, with scenario 2's history loaded: *"Write a LinkedIn post announcing our Q3 launch, based on doc Q3-plan."* All the post rules pass, and every call stays within budget.

We'll also run a few variations of our own against your core.

**Out of scope:** other channels (Teams, SMS, etc.), auth, UI polish, deployment.

> If something is unclear, make a reasonable assumption, note it in NOTES.md, and keep going. If you run out of time, stop and write down what's left and what you'd do next. An honest partial result beats a rushed complete one.

## **How to work**

* Use **both Claude Code and Codex**. How you use them is up to you.  
* Work in a fresh directory used only for this task, so your transcripts contain only this work.  
* Commit as you go. Your first commit is a short plan.  
* **Keep every session.** Don't trim, curate, or restart to hide dead ends.

## **What to submit**

1. **The repo:** a zip that includes `.git`, or a GitHub repo link (public or private; if private, share it with @**skblazel**).  
2. **README.md:** how to run the core, the Slack app, and the scenario script, and how to add a new capability.  
3. **NOTES.md**, one page:  
   * your design, the trade-offs, what doesn't work, and roughly how many hours you spent  
   * 2 places where Claude Code or Codex was wrong and how you caught it  
   * 1 AI suggestion you rejected, and why  
4. **All AI session transcripts,** unedited, with API keys redacted. Send them separately; don't commit them to the repo:  
   * Claude Code: this project's folder under `~/.claude/projects/` (named after your project path), including the `subagents/` subfolders  
   * Codex: the `rollout-*.jsonl` files under `~/.codex/sessions/` for the days you worked  
   * any other AI tool you used: export and include it  
5. **A screen recording,** 2 minutes at most, showing scenario 1 working in your real main chat and your real Slack DM.

We use your submission only to evaluate your application.

## **Next step**

A 45-minute call where you walk us through your sessions.

