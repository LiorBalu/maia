# Maia, הפרונטאנד החדש

ממשק הניהול של Maia: אדמין רואה את כל העסקים והמשתמשים; משתמש של עסק רואה את העסק שלו (תיבת הודעות, מאגר ידע, יומן, הבוט, הגדרות). React + TypeScript + Vite, עברית ו־RTL כברירת מחדל, עיצוב בצבעי תכלת.

## הרצה מקומית

דרישות: Node 20, והבקאנד של Maia רץ על `http://localhost:8000`.

```bash
npm install
npm run dev        # http://localhost:5173
```

שרת הפיתוח מעביר כל קריאה ל־`/api` ו־`/management` לבקאנד (proxy ב־`vite.config.ts`), לכן אין צורך בהגדרת כתובת. כשמגישים את ה־build ממקום אחר מגדירים `VITE_API_URL` (ראי `.env.example`).

## בדיקות

```bash
npm run typecheck  # tsc --noEmit
npm run build      # typecheck + vite build → dist/
```

## מבנה

- `src/api/` קריאות לבקאנד (axios, טוקן Bearer, 401 → יציאה): `auth`, `tenants`, `conversations`, `customers`, `knowledge`, `bot`, `calendar`, `settings`, `admin`.
- `src/auth/` session (JWT ב־localStorage), AuthProvider, guards של תפקיד/עסק.
- `src/hooks/` SSE של השיחות (`useConversationStream`), חלון OAuth של Google (`useGooglePopup`), debounce.
- `src/layout/` המעטפת: תפריט צד, מתג עסקים, כותרת.
- `src/components/` רכיבים משותפים: טפסים (`Form.tsx`: שדה, מתג, כפתור שמירה, העתקה), לשוניות, תגים, אווטאר, מצבי טעינה/שגיאה.
- `src/pages/admin/` מסכי אדמין: עסקים (רשימה + יצירה), משתמשים (רשימה + יצירה + מחיקה).
- `src/pages/tenant/` מסכי עסק: `InboxPage` + `inbox/`, `KnowledgePage` + `knowledge/`, `BotPage` + `bot/`, `CalendarPage` + `calendar/` (לוח פגישות בסגנון Google Calendar: חודש / שבוע / יום, דיאלוג פגישה), `SettingsPage` + `settings/`.
- `src/lib/` תאריכים (UTC מהשרת → שעון ישראל), הודעות שגיאה, הכתובת הציבורית ל־webhooks.
- `src/i18n/he.json` כל הטקסטים.
- `tailwind.config.ts` טוקני העיצוב (`brand` = תכלת).

## הערות

- השרת מחזיר תאריכים ב־UTC בלי אזור זמן, בשני כתיבים; `src/lib/dates.ts` מפענח את שניהם.
- עדכון חי: כל שמירת שיחה בבקאנד משדרת `message_update` עם השיחה המלאה ב־SSE (`/management/v1/sse/{tenant}/conversations?token=`).
- "בדקי את הבוט" שולח דרך ערוץ ה־web (`/api/async/web/{tenant}`), אותו צינור כמו וואטסאפ ומייל.
- חיבור Google (יומן / Gmail) נפתח בחלון קופץ; דף ה־callback של השרת שולח `auth_success` ל־opener.
