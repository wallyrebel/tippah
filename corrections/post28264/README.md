# Fixed editorial cleanup for Tippah post 28264

Only the three reviewed literal instruction substrings in fact-ledger.json may
be deleted. Existing price values, as-of dates, product claims, links, images and
research-time unavailable-price wording are retained. No material factual change
or price assertion is made, so no factual correction note is appended.

The managed runner permits only this exact post, host, branch, current identity,
modification time, before digests and reviewed after digest. It derives content
from authenticated current raw text, preserving every byte except the fixed
literal deletions. Title, excerpt, metadata and publication fields are never sent.

Offline guard tests precede authenticated preflight/dry-run with zero writes.
Apply requires the exact successful dry receipt, identical implementation and
ledger, fresh identity/rendered/raw checks and an immediate read before one POST.
Denial stops; uncertainty reconciles the same post without automatic retry.

Only existing managed Actions credentials are used. Concurrency shares the
existing tippah-wordpress-publisher lock. Main and reliability fix b350eb3 remain
unchanged; no collector, newsletter, player or RSS workflow edits or reruns.
