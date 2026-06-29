# App Spec v2 — контракт описания приложения (Stage B)

Выход стадии анализа (`iosforge/mvp/analyze.py`, `analyze()`): по скриншотам обхода
(`screens/*.png` + `screens.json`) локальный `claude` строит **полное универсальное
описание приложения** для передачи на разработку. Два артефакта в `runs/<ts>/`:

- `app_spec.json` — машиночитаемый, для codegen (Stage C/D).
- `SPEC.md` — человекочитаемый, **рендерится детерминированно из JSON** (`_render_spec_md`).

LLM пишет только `app_spec.json`; `SPEC.md` генерится из него — поэтому они всегда
консистентны. Спек валидируется (`_validate_app_spec`): `screens` непустой + наличие всех
секций из `_REQUIRED_SECTIONS`.

## Универсальность
Поле `app_type` (game / photo-editor / social / utility / content-subscription / e-commerce /
productivity / health / education / media / other) задаёт тип и активирует условное
наполнение блоков (для игр — правила/уровни в `business_logic.domain_rules`; для фото —
пресеты/фильтры в `content.content_inventory` и `content.content_to_seed`; и т.д.).

## Топ-уровневые секции (все обязательны; пусто = `[]`/`{}`/`""`, не опускать)

| Ключ | Назначение |
|------|-----------|
| `app_name`, `package`, `app_type` | идентичность + тип (драйвер условных блоков) |
| `one_liner`, `description`, `how_it_works` | что это и как работает (основной цикл) |
| `target_audience`, `platforms` | аудитория, платформы |
| `market_research` | аналоги, конвенции категории, нормы монетизации, `sources` (веб-ресёрч) |
| `business_logic` | `summary`, `domain_rules` (правила/игра), `workflows`, `state_machine` |
| `screens[]` | по экрану: `purpose`, `route`, `components{type,role,data}`, `states` (loading/empty/error), `dynamic_content`, `navigates_to` |
| `design` | `colors[]`, `typography`, spacing, `iconography`, `dark_mode`, `motion`, `ios_adaptation` |
| `navigation` | `type` (tab/stack/drawer), `map[]`, `deep_links` |
| `content` | `data_model[]`, `content_inventory[]`, **`content_to_seed[]`** (что наполнять), `persistence` |
| `monetization` | `model`, `paywalls[]`, `packages[]`, `ads[]`, `free_vs_premium[]` |
| `backend` | `backend_needed`, `admin_panel_needed`+`admin_scope`, `auth`, `user_roles`, `apis`, `push_notifications`, `cloud_sync` |
| `permissions[]` | разрешения устройства + причина |
| `integrations[]` | сторонние SDK (оплаты/аналитика/auth/реклама) |
| `cross_cutting` | `localization`, `onboarding`, `analytics_events`, `accessibility`, `legal` |
| `analysis_quality` | `assumptions`, `open_questions`, `coverage_gaps`, `confidence` — честно, что не увидел краулер |
| `acceptance_criteria[]` | чек-лист «приложение готово, если …» (в т.ч. для проверки в Chrome-web) |

Точная JSON-форма каждого блока — в `ANALYZE_PROMPT` (`iosforge/mvp/analyze.py`).

## Веб-ресёрч
Промпт просит `claude` исследовать в интернете устройство похожих приложений и проставить
`market_research.sources`. Если веб-доступа нет — секция заполняется из знаний модели, `sources`
пустой, факт фиксируется в `analysis_quality`. Жёсткой зависимости от сети нет.

## Совместимость и долг
- Контракт `screens.json` (Stage A) не менялся; `app_spec.json` — расширен (старые потребители
  читают `screens`/`app_name` как раньше).
- Промпт анализа пока inline (как `claude_gen`/`compliance`); вынос в `providers/promptset`
  (SPEC §6) — отложенный техдолг.

## Тестирование сгенерированного приложения
Договорённость: проверяем как **Flutter-web в Chrome** (без сборки APK/iOS — быстрее). Критерии
берутся из `acceptance_criteria`. Реализация web-прогона — отдельная задача (Stage D/E).
