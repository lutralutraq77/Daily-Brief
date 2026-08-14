Style guidance for the brief.

<!--
THIS FILE IS ONLY USED WHEN "engine" IS "claude" OR "auto" IN config.json.

The default engine is "local", which builds the brief from keyless APIs with no
credentials at all — it never reads this file.

When the Claude engine IS active, the data has ALREADY been fetched before Claude
is called. Claude receives it as JSON and only writes the prose, so it cannot
invent a headline or a temperature. What you write below is style guidance, not
a research brief.

To change WHAT is collected, edit "sections" in config.json, not this file.
-->

Keep it to one screen. I would rather it were too short than too long.

- Lead with the single most useful fact, not a greeting.
- Headlines only. Do not summarise the articles — I will click if I care.
- Bold anything that needs a decision from me today.
- Say "nothing notable" rather than padding a section out.
- No motivational closing line. If there is nothing to say, stop.
- British English, and Celsius.
