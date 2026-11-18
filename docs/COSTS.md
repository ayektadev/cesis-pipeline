# What a mission costs, and why

Running this pipeline spends real money. A sixty-to-seventy patient mission
costs somewhere around **twelve to fifteen dollars**, or roughly **twenty
cents per patient**. Sierra Leone 2025 came to about $14 for 66 packets.

That is the expected number for this design, not a sign anything went wrong.
This document explains where it goes, what was genuinely wasted, and what
could be done about it.

Every run now prints its own token counts and an estimated cost, per packet
as it goes and again at the end. Trust the token counts; they come from the
API. The dollar figure is derived from a price hard-coded in `extract.py`
and will drift as pricing changes.

## Where the money goes

The pipeline works in two stages, and only the second one costs anything.

Stage one classifies each page — deciding whether it is an OR Record or a
Consent form — using local OCR on your own machine. It is free and it does
real work: pages with no extraction prompt, chiefly Consent and PACU Orders,
are never sent anywhere. Roughly one page in six is filtered out this way.

Stage two sends one image plus one text prompt to the model, per remaining
page, and gets structured JSON back. That is the entire bill. A full mission
is somewhere in the region of five to six hundred of these calls.

Inside a single call, the split is lopsided:

| | approx. tokens | approx. share |
|---|---|---|
| the page image | ~2,500 | 60% of input |
| the prompt text | ~1,300–2,200 | 40% of input |
| the JSON that comes back | ~400–700 | — |

Output tokens are priced several times higher than input, so the returned
JSON costs roughly as much as the whole prompt despite being much smaller.
Both matter; neither dominates.

The consequence worth internalising is that **cost scales with pages, not
with patients**. A packet with three progress notes costs more than one with
none. Nothing is charged twice for the same page.

## What was actually wasted on Sierra Leone

Being specific, since "roughly $14" hides the answer.

**About $1.40, re-reading 66 discharge pages.** The original prompt confused
the "Follow up on ___" clinic date with the actual discharge date, which
inflated length of stay on nine patients. Fixing it meant paying to read
those pages again.

Two things about that are worth keeping. First, only the RN Discharge page
was re-run, not the whole packet — re-running everything would have cost
about $12 instead of $1.40, and would also have re-rolled answers that had
already been checked against paper. Targeted re-extraction is the right
instinct whenever a prompt changes. Second, the vet gate did not catch this
one, and could not have: the bug only becomes visible as a pattern across
many patients, and the gate looks at five. That is a real limitation of the
gate, not a failure to use it properly.

**A small amount on calls that were paid for and then thrown away.** Early
in the build, responses were lost to a thinking-block bug and to occasional
malformed JSON. Both are fixed. Probably under a dollar in total, but it is
the category worth watching, because a crash after the call is the one kind
of spend that buys nothing at all.

**Nothing on the vet gate.** The five vetted packets are reused by the full
run and never re-billed. Vetting is free in the sense that matters.

## What could make it cheaper

Ranked by how much they save against how much they risk.

**Prompt caching — already implemented, saves roughly 10-15%.** About 1,100
tokens of every prompt are byte-identical on every call of the run: the
integrity rules and the mission context. Those are now marked cacheable, so
they are paid for once and served at a tenth of the price thereafter. It
does not halve the bill, because the image is unique per page and cannot be
cached, and cache entries expire after a few minutes of disuse. If the
endpoint rejects the caching directive for any reason, the pipeline retries
without it rather than failing the run.

**The Batch API — roughly halves the bill, not yet implemented.** Submitting
the calls as an asynchronous batch is priced at about 50% of standard rates,
which would take a mission from $14 to around $7. A mission run is already a
background job that takes an hour, so the asynchrony costs almost nothing in
practice. This is the single largest available saving and the obvious next
piece of work. The vet run should stay synchronous — you want those five
packets back immediately.

**Sending fewer pages — unknown saving, needs data first.** Some page types
may be contributing almost nothing. The Floor Flowsheet, for instance, feeds
one column that is very often blank. If a page type turns out to be empty on
the great majority of packets, not sending it saves its full per-call cost.
Worth measuring across a completed mission before deciding; do not guess.

**Smaller images — up to 20%, but do not do this blind.** Page images are
rendered at 1,568 pixels on the long edge, which is where most of the input
cost sits. Halving that would roughly halve the image tokens. It would also
be exactly the wrong economy if it costs accuracy on handwriting, which is
the hardest thing the model is being asked to do and the thing this whole
pipeline exists to get right. If it is tried, it must be measured at a vet
gate against the same packets read at full size, not adopted on the argument
that it looks cheaper.

Related, and already fixed: pages used to be rendered at 300 DPI, which
upscaled 200 DPI scans and made the files larger without adding any
information the model could use. Rendering at native resolution and capping
the long edge was a straight saving with no downside.

**A cheaper model for simple pages — not recommended.** Some pages carry
four printed fields and little handwriting, and could plausibly be read by a
smaller, cheaper model. The reason to resist is that the failure mode is
silent. A misread time does not announce itself; it produces a plausible
number that someone then has to catch by eye against paper. Saving a few
dollars per mission is not worth raising the odds of that, unless a vet gate
demonstrates the cheaper model is genuinely as good on this handwriting.

## Keeping the bill predictable

Run `--vet 5` first, always. It bounds the cost of a systematic mistake to
five packets.

Never delete `raw_json_output/` to tidy up. Those files are what makes a run
resumable and re-billing impossible. Deleting one means paying to read that
packet again.

Watch the running total that now prints after each packet. If it is climbing
faster than about twenty cents a packet, something is being sent that
shouldn't be, and stopping early costs nothing.
