# Frontend UI/UX Requirements

All frontend work must preserve a production-grade investment intelligence experience. The product must feel like premium financial SaaS, not a generic admin dashboard or raw CRUD interface.

## Design Goals

- Clean, modern, responsive desktop/tablet/mobile layouts.
- Strong information hierarchy for portfolio and research workflows.
- Accessible typography, keyboard navigation, semantic HTML, visible focus states, and adequate contrast.
- Consistent spacing, terminology, component behavior, and data presentation.
- Professional financial tables, charts, risk indicators, and BUY / HOLD / SELL status presentation.
- Minimal animation, restrained color, and no visually noisy dashboard composition.
- Financial readability takes priority over decorative visuals.

## Design System

Frontend implementation must use reusable components for:

- typography scale
- spacing scale
- cards
- buttons
- inputs
- badges
- tabs
- tables
- drawers
- dialogs
- tooltips
- skeleton loading
- empty states
- error states
- status indicators
- responsive navigation
- chart containers

Tailwind CSS and shadcn/ui may be introduced only when compatible with the current Next.js setup and when the added dependency weight is justified. Large UI libraries should not be introduced by default.

## Application Shell

The application must use a professional shell with:

- collapsible sidebar navigation
- top command/search area
- notification area
- user/account menu
- responsive mobile navigation

Primary navigation should include Dashboard, Portfolio, Research, Screener, Watchlist, Insights, Brokers, and Settings as the product matures.

## Phase 1 Frontend Scope

Phase 1 frontend must provide professional UI for:

- application shell
- sidebar navigation
- dashboard
- portfolio list/client selection
- portfolio detail
- holdings table
- portfolio summary cards
- allocation visualization
- mock broker information
- demo-data indicator
- loading, error, and empty states

Use the actual Phase 1 Portfolio API wherever endpoints exist. Do not hard-code portfolio results when backend endpoints are available. Mock data must be clearly labelled as demo data and never presented as live market data.

## Data Presentation

Important financial areas should be designed to display:

- last updated time
- source
- REAL-TIME, DELAYED, END-OF-DAY, or RESEARCH UPDATE freshness labels when real data exists

Phase 1 must clearly display Demo data.

## Future Pages

The stock detail architecture must be able to support:

- company header
- current price, daily movement, market cap
- AI Opportunity Score
- BUY / ADD / HOLD / TRIM / SELL
- tabs for Overview, Financials, Valuation, Growth, Orders & Backlog, News, Analysts, Institutions, Insiders, and Risks
- valuation, growth, quality, catalyst, analyst, institutional, and risk scores

Do not implement fake analysis before the relevant backend logic exists.

## Error And Loading Experience

Never show blank pages while loading. Use skeleton cards, skeleton tables, and loading indicators.

API errors must show professional user-safe messages. Do not expose stack traces, SQL errors, internal service names, secrets, or broker credentials. Show correlation IDs when available.

## Security Constraints

Never request, display, or store broker passwords, Azure credentials, API keys, access tokens, or other secrets in the frontend. Broker integration screens must remain placeholder/demo-only until real provider flows are designed.
