# Where this goes in two years

Today the model is the product and the guardrails are an afterthought. I think that flips: models get cheaper and more interchangeable, and the part that has to hold still is whatever decides what an agent is actually allowed to do, and can show its work.

Nobody's going to audit a model. They'll audit a function — takes a proposal and some verified facts, returns a decision, and when someone wants to rerun last Tuesday's call, gives the same answer back. That's the whole bet behind making this one pure and replayable.

Authority has to be data, not a claim. The agent says "10 records"; the system knows it's 600. Anything an agent asserts about itself is evidence at best, never ground truth, and systems that trust the number they're handed will keep getting surprised by it.

Approval has to mean something narrower than it usually does. A human clicking "approve" on a summary isn't control. A human signing the exact action, evidence attached, with a record that they did it, is. The queue should stay small, and every item in it should matter.

Policy should get versioned and tested the way code does: change a limit, replay last month against it, see what would've changed, run it in shadow mode before it's live.

Eventually this crosses organizational lines. When your agent calls mine, I'm not going to trust its description of itself — I'll want a signed record of what it was allowed to do and who agreed to it.

Where I'd push back on myself: the windows here are crude, and real abuse is slower and spread across more agents than this catches today (see the known gaps). If policy ever gets learned instead of written, it'll drift permissive quietly, and the audit trail might be the only thing that notices.
