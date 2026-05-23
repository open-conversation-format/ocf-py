# Cursor session storage — schema map

Notes on where Cursor (IDE, current Anysphere build) keeps chat / composer
data, based on direct inspection of `state.vscdb` on a real machine plus a
review of how other extractors (cursor-chat-browser, cursor-view,
cursor-history, cursor-trace-exporter) navigate the same files. This is
meant as a working reference for the `cursor` adapter, not a Cursor spec.

## Storage layout

```
%APPDATA%/Cursor/User/
├── globalStorage/
│   ├── state.vscdb              ← the big one (~1.4 GB on a power user)
│   ├── state.vscdb-shm          ← SQLite shared-memory
│   ├── state.vscdb-wal          ← write-ahead log; must be present to read
│   └── state.vscdb.backup       ← previous snapshot
└── workspaceStorage/
    └── <hash>/                  ← one folder per workspace ever opened
        ├── workspace.json       ← { "folder": "..." } or { "workspace": "....code-workspace" }
        └── state.vscdb          ← per-workspace settings + composer index
```

Always open via `file:...?mode=ro&immutable=1` URI — the live DB is held by
the running Cursor process. A naive `cp` followed by `sqlite3` returns
empty tables because the journal isn't replayed; the project's
`utils.sqlite_ro.open_ro` does it right.

## Global DB — `state.vscdb`

Two tables, both key/value blobs:

| Table          | Rows (sample) | Purpose                                  |
| -------------- | ------------: | ---------------------------------------- |
| `ItemTable`    |           347 | VS Code-style settings, UI state         |
| `cursorDiskKV` |       102 997 | Cursor's own append-only chat store      |

### `cursorDiskKV` key prefixes

| Prefix                          | Count (sample) | Holds                                                                                |
| ------------------------------- | -------------: | ------------------------------------------------------------------------------------ |
| `agentKv:blob:<sha256>`         |         57 556 | Raw agent request blobs (JSON `{role, content}`). Contains `Workspace Path:` strings inside `<user_info>` |
| `bubbleId:<composerId>:<bubId>` |         35 823 | One row per chat message ("bubble"). Sole source of message content                  |
| `checkpointId:<...>`            |          2 814 | Checkpoint snapshots for "rewind"                                                    |
| `codeBlockPartialInlineDiffFates` |        1 455 | Apply/reject state of inline diff suggestions                                        |
| `codeBlockDiff:<...>`           |          1 066 | Diff blobs                                                                           |
| `ofsContent:<...>`              |            546 | File snapshots referenced from bubbles (`originalFileStates`)                        |
| `messageRequestContext:<composerId>:<reqId|WARM_SUBMIT>` | 397 | Context payload that was sent with each request — has `projectLayouts`, `gitStatusRaw`, `ideEditorsState` |
| `composerData:<composerId>`     |            171 | Composer-level metadata + bubble order                                               |
| `composer.content.<sha256>`     |        ~ huge  | Frozen composer-content snapshots, ID'd by content hash                              |
| `inlineDiff:<...>`              |             32 | Inline diff state                                                                    |

### `composerData:<composerId>` — top-level fields

The single most important record. Sampled keys (Cursor `_v=10`):

```
composerId               name                    subtitle
createdAt  lastUpdatedAt latestChatGenerationUUID
status     unifiedMode   forceMode               capabilities[]
isAgentic  isArchived    isDraft   isSpec        isBestOfNParent
modelConfig            { modelName, maxMode }       ← active model
latestConversationSummary { model?, title? }        ← often empty
fullConversationHeadersOnly[]                       ← bubble order (length = bubble count)
context                 { composers, quotes, selectedCommits, ... }
codeBlockData          { <fileURI>: ... }
originalFileStates     { <fileURI>: ... }
contextTokenLimit      contextTokensUsed   contextUsagePercent
todos[]                firstTodoWriteBubble
subComposerIds[]
richText               text                       ← current input box content
```

Workspace folder is **not** stored here. That's the source of the project
mapping problem.

### `bubbleId:<composerId>:<bubbleId>` — message record

Each bubble has ~70 keys. Useful subset:

```
type     ← 1 = user, 2 = assistant (others = system)
text     richText
modelInfo            { modelName }   ← per-bubble fallback (~8% set)
toolFormerData       { name, rawArgs, params, result, modelCallId, status }
toolResults[]        supportedTools[]
attachedCodeChunks[] attachedFolders[]   attachedFoldersNew[]
codebaseContextChunks[]
contextPieces[]      cursorRules[]      docsReferences[]
images[]             notepads[]         pullRequests[]
gitDiffs[]           humanChanges[]     diffHistories[]
allThinkingBlocks[]
checkpointId         requestId          tokenCount
isAgentic            isPlanExecution    isRefunded
```

### Other notable keys in `ItemTable`

```
workbench.panel.aichat.view.aichat.chatdata        ← legacy chat data
workbench.panel.chat.view.chat.chatdata            ← newer key (post v44.9)
aiService.prompts                                  ← prompt history
aiService.generations                              ← completion history
cursor/agentLayout.*                               ← UI layout
```

## Workspace DBs — `workspaceStorage/<hash>/state.vscdb`

Same two-table shape, but `cursorDiskKV` is usually empty here. The
chat data lives in the global DB; the workspace DB only holds:

| `ItemTable` key                                                  | Holds                                                                                   |
| ---------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `composer.composerData`                                          | `{ allComposers: [{composerId, type, createdAt, name?, ...}], selectedComposerIds, lastFocusedComposerIds }` |
| `workbench.panel.composerChatViewPane.<workspaceViewUUID>`       | View state for the composer panel                                                       |
| `workbench.panel.aichat.<workspaceViewUUID>.numberOfVisibleViews`| Visibility count                                                                        |
| `workbench.backgroundComposer.workspacePersistentData`           | Background-composer state                                                               |
| `cursor/needsComposerInitialOpening`                             | One-time bootstrap flag                                                                 |
| `cursor/workspaceEligibleForSnippetLearning`                     | Telemetry opt-in                                                                        |
| `cursorAuth/workspaceOpenedDate`                                 | First-open timestamp                                                                    |

`workspace.json` next to each `state.vscdb`:

```json
{ "folder": "file:///c%3A/Development/Projekte/Python/Foo" }
```

or, for multi-root:

```json
{ "workspace": "file:///c%3A/.../foo.code-workspace" }
```

The `.code-workspace` form is a JSON file that itself lists `folders[]`.
Neither cursor-chat-browser nor cursor-view handle this; the current
`cursor` adapter strips `.code-workspace` from the name but doesn't
resolve `folders[]`.

## Composer → workspace mapping — sources, ranked

Each row is "ways to discover which workspace a composer belonged to,
given only a `composerId`". Sample numbers from this machine: 170
composers in global DB, 96 directly mappable, 67 with cross-DB confirmation.

| # | Source                                                                  | Reliability | Used by                                        |
| - | ----------------------------------------------------------------------- | ----------- | ---------------------------------------------- |
| 1 | `workspaceStorage/<ws>/state.vscdb` → `composer.composerData.allComposers[].composerId` | direct, deterministic | cursor-chat-browser; **not yet used by ocf-py** |
| 2 | `cursorDiskKV: messageRequestContext:<composerId>:*` → `projectLayouts[].rootPath` | direct, deterministic; only present once a request was made | cursor-chat-browser; **not yet used by ocf-py** |
| 3 | `composerData.workspaceFolder`                                          | direct, but usually `null` on this build  | ocf-py (primary)                               |
| 4 | `composerData.allAttachedFileCodeChunksUris` / `codeBlockData` / `originalFileStates` → common path prefix | heuristic, fails for chats without file attachments | cursor-view (commonprefix only)                |
| 5 | viewPane / `selectedComposerIds` cross-reference                        | heuristic, **novel to ocf-py** (commit 3eb84df) | ocf-py (workspace map fallback)                |
| 6 | `agentKv:blob:<sha>` → grep for `Workspace Path:` inside `<user_info>`  | heuristic, requires content scan + reverse map to composer | nobody                                         |

The fact that sources 1 and 2 are deterministic and underused is the
single biggest coverage win available — adding either would explain
most of the 101/268 (38%) of sessions where `project` came out empty.

## Model detection — fallback chain

Currently in `cursor.py` (in order):

1. `composerData.latestConversationSummary.model`
2. `composerData.model | modelId | modelName`
3. `composerData.modelConfig.modelName`  ← newest Cursor key, this works
4. → empty

Not yet checked:

5. `bubbleId:*.modelInfo.modelName`  ← per-bubble; ~8% set even when composer-level is empty

No other reviewed tool (cursor-view, cursor-chat-browser, S2thend,
Cedriccmh) extracts the model at all. ocf-py is already ahead of the
field here.

## Schema drift watch

cursor-chat-browser issues record these breaks:

- Cursor **v44.9** moved chat data location into `globalStorage`; the
  legacy chat-vs-composer distinction collapsed (issue #18).
- Cursor **0.48.6** changed the workspaceStorage layout, producing empty
  dir names (issue #27).
- The newer `workbench.panel.chat.view.chat.chatdata` key (no `ai`)
  appeared at some point post-v44.9 and is only read by S2thend.

The defensive pattern from S2thend (probe `sqlite_master` for table /
key existence before reading) is worth porting to the adapter.

## Where ocf-py stands vs. the field

Recap of what the survey found:

| Capability                                          | ocf-py | cursor-chat-browser | cursor-view | S2thend | Cedriccmh |
| --------------------------------------------------- | :----: | :-----------------: | :---------: | :-----: | :-------: |
| Model name resolution                               |   ✅   |          ❌         |     ❌      |   ❌    |    ❌     |
| Workspace via `messageRequestContext.projectLayouts`|   ❌   |          ✅         |     ❌      |   ❌    |    ❌     |
| Workspace via per-workspace `composer.composerData` |   ❌   |          ✅         |     ❌      |   ❌    |    ❌     |
| Workspace via viewPane heuristic                    |   ✅   |          ❌         |     ❌      |   ❌    |    ❌     |
| Multi-root `.code-workspace` resolution             |   ❌   |          ❌         |     ❌      |   ❌    |    ❌     |
| Structured `toolFormerData` parsing                 |   ✅   |          ❌         |     ❌      |   ❌    |  ❌ (raw) |
| Skip-empty-composer hygiene                         |   ✅   |          ❌         |     ❌      |   ❌    |    ❌     |
| Newer `panel.chat.view.chat.chatdata` key           |   ❌   |          ❌         |     ❌      |   ✅    |    ❌     |
