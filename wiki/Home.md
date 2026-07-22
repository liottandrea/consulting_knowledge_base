---
type: dashboard
title: Home
updated_at: '2026-07-20T08:59:50.656928+00:00'
sources: []
---

# 🏠 Consulting KB — Home

Live dashboard of the wiki. Requires the **Dataview** plugin (Settings →
Community plugins → install "Dataview"). Open this note in **Reading view**
(⌘E toggles) to see the tables render.

> [[index|📇 Full catalogue (index.md)]] · [[log|🪵 Operations log]]

---

## 📊 Overview

```dataview
TABLE WITHOUT ID type AS "Type", length(rows) AS "Pages"
FROM "entities" OR "concepts" OR "engagements" OR "sources" OR "synthesis"
GROUP BY type
SORT length(rows) DESC
```

## 🕑 Recently updated

```dataview
TABLE WITHOUT ID file.link AS "Page", type AS "Type", updated_at AS "Updated"
FROM "entities" OR "concepts" OR "engagements" OR "synthesis"
SORT updated_at DESC
LIMIT 15
```

---

## 🏢 Clients & companies

```dataview
TABLE WITHOUT ID file.link AS "Entity", entity_type AS "Kind", tags AS "Tags"
FROM "entities"
WHERE entity_type = "client" OR entity_type = "company"
SORT file.name ASC
```

## 🤝 Engagements

```dataview
TABLE WITHOUT ID file.link AS "Engagement", sources AS "Sources", updated_at AS "Updated"
FROM "engagements"
SORT updated_at DESC
```

## 🧰 Tools, platforms & frameworks

```dataview
TABLE WITHOUT ID file.link AS "Entity", entity_type AS "Kind"
FROM "entities"
WHERE entity_type = "tool" OR entity_type = "platform" OR entity_type = "framework"
SORT entity_type ASC, file.name ASC
```

## 💡 Concepts by domain

```dataview
TABLE WITHOUT ID file.link AS "Concept", domain AS "Domain", tags AS "Tags"
FROM "concepts"
SORT domain ASC, file.name ASC
```

---

## 🔎 By topic (edit the tag)

Change `"ai-governance"` below to any tag you use often:

```dataview
LIST
FROM "entities" OR "concepts" OR "engagements"
WHERE contains(tags, "ai-governance")
SORT file.name ASC
```

## 📄 Source documents

```dataview
TABLE WITHOUT ID file.link AS "Source page", location AS "Location", updated_at AS "Ingested"
FROM "sources"
SORT updated_at DESC
```

---

## 🧹 Housekeeping — pages needing attention

Pages missing a `type` or `updated_at` (candidates for `wiki_lint.py --fix`):

```dataview
LIST
FROM "entities" OR "concepts" OR "engagements" OR "sources" OR "synthesis"
WHERE !type OR !updated_at
SORT file.name ASC
```
