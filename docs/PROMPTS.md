# Things to ask your AI once WHOOP is connected

Copy any of these into Claude, ChatGPT, Cursor — wherever you've connected whoop-mcp. They're grouped by what you're trying to get out of your data. The model picks the right tools automatically.

## Every morning

- *How am I doing today?*
- *Should I train hard today or take it easy? Use my recovery, sleep debt, and this week's load.*
- *How did I sleep last night, and what one thing should I change tonight?*
- *Is my HRV trending the right way this week?*

## The big picture

- *Give me a full overview of my health this quarter.*
- *What are my personal records this year — best recovery, biggest workout, longest sleep, longest green streak?*
- *Compare this month to last month across everything. What improved, what declined?*
- *Show me my weekly report and point at the specific days that dragged the averages down.*

## Finding your patterns

- *What actually affects my recovery? Look at the correlations over the last 90 days.*
- *Does hard training hurt my sleep the same night? Check strain vs that night's sleep.*
- *Find my worst nights this month and tell me what they had in common.*
- *Which days of the week do I recover best? Use the daily tables.*

## Training

- *Am I overtraining? Check my acute:chronic load ratio and recovery trend.*
- *Break down my workouts by sport this month — time, calories, average strain.*
- *Plan next week's training day by day based on my current load and recovery pattern.*
- *How did my heart-rate zones look across my runs this month?*

## Sleep deep-dives

- *Act as my sleep coach: analyze the last two weeks and give me a prioritized action list.*
- *Show my overnight heart-rate curve from last night. When did it bottom out?* (uses the minute-level sleep stream)
- *How much sleep debt am I carrying, and how many early nights would clear it?*
- *Is my sleep consistency getting better or worse since I started the new schedule?*

## Data nerd mode

- *Export my last two years of WHOOP data and tell me where the files are.*
- *Pull the raw API record for last night's sleep — I want to see every field.*
- *Graph-ready: give me the daily recovery, HRV, and strain table for the last 60 days.*
- *Which of my recovery scores this quarter were statistical outliers?*

## Built-in prompts

Your MCP client also exposes four ready-made prompts — `morning_readiness`, `weekly_review`, `sleep_coach`, and `training_planner` — in its prompt picker (in Claude Desktop: the ＋ menu).

## No WHOOP yet?

Run the server with `--demo` and every prompt above works against a realistic generated athlete — patterns, correlations, and all.
