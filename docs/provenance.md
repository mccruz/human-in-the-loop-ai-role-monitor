# Provenance and publication boundary

This repository is a new, independently authored implementation with a clean Git history. It does not inherit commits, branches, generated data, runtime configuration, or vendor bundles from a private repository.

## Design provenance

The generic behavioral requirements came from independently authored monitoring work: stable role identity, SQLite persistence, explicit review state, atomic handoff files, receipt verification, retry isolation, and deterministic reporting. Those concepts were reimplemented behind new public module boundaries and tested with fictional records.

The ATS mappings and payload shapes are based on public vendor documentation:

- [Greenhouse Job Board API](https://developers.greenhouse.io/job-board.html)
- [Greenhouse job-post URL guidance](https://support.greenhouse.io/hc/en-us/articles/360020774051-Job-post-URL-for-non-Greenhouse-hosted-job-posts)
- [Lever Postings API](https://github.com/lever/postings-api)
- [Ashby Public Job Posting API](https://developers.ashbyhq.com/docs/public-job-posting-api)
- [SmartRecruiters public Posting API](https://developers.smartrecruiters.com/docs/endpoints)
- [Workable public published-jobs endpoint](https://help.workable.com/hc/en-us/articles/115012771647-Using-the-Workable-API-to-create-a-careers-page)
- [Recruitee Careers Site API](https://docs.recruitee.com/reference/intro-to-careers-site-api)
- [Recruitee feed field reference](https://docs.recruitee.com/docs/feed)
- [Telegram Bot API `sendMessage`](https://core.telegram.org/bots/api#sendmessage)
- [Telegram BotFather tutorial](https://core.telegram.org/bots/tutorial)

All committed fixtures are synthetic and were created for this repository. Telegram tests use injected senders and deterministic non-secret placeholders. No real job-search dataset, personal eligibility rule, live service route, credential, chat destination, production path, or private task-system schema is included.
