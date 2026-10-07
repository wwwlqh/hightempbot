---
type: meta
title: "Dashboard"
updated: 2026-06-08
tags: [meta, dashboard]
---

# Wiki Dashboard

## Recent Activity

```dataview
TABLE type, status, updated FROM "wiki" SORT updated DESC LIMIT 15
```

## Seed Pages (Need Development)

```dataview
LIST FROM "wiki" WHERE status = "seed" SORT updated ASC
```

## Entities Missing Sources

```dataview
LIST FROM "wiki/entities" WHERE !sources OR length(sources) = 0
```

## Open Questions

```dataview
LIST FROM "wiki/questions" WHERE answer_quality = "draft" SORT created DESC
```

## Decision Log (All)

```dataview
TABLE decision_date, status, title FROM "wiki/decisions" SORT decision_date DESC
```

## Postmortems

```dataview
TABLE incident_date, status, severity FROM "wiki/postmortems" SORT incident_date DESC
```
