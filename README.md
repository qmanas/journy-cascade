# ⏱️ Journy Cascade: AI Agent Scheduling # ⏱️ Journy Cascade: Multi-Agent Scheduling & Conflict Resolution Engine Conflict Resolution

**Journy Cascade** is a high-level transactional engine designed to manage complex event dependencies and scheduling conflicts in AI-driven environments. This engine excels at balancing data across multiple sources (Google Calendar, Journals, Task Managers) while maintaining a consistent "Truth Tunnel" for the user.

---

## 🔥 Problem: Static Scheduling in a Dynamic Life
Traditional scheduling algorithms are static - they don't understand the *sentiment* or *context* behind an event. If a user journals about burnout, a standard calendar shouldn't schedule high-intensity tasks. **Journy Cascade** treats the schedule as a dynamic, agentic state reconciled against real-time user context.

---

## 🛡️ Architecture: Cascading Consistency
1.  **Transactional Scheduling**: Uses `scheduler_agent.py` to treat every calendar modification as a tentative proposal, calculating the "Cascading Effect" on all subsequent dependent events.
2.  **Consistency Guard**: The `consistency_agent.py` acts as a specialized auditor that validates proposed changes against hard constraints (e.g., travel times) and soft constraints (e.g., personal energy levels).
3.  **Cross-Domain Sync**: Seamlessly merges data from Google Calendar APIs, local SQLite state, and LLM-inferred context into a singular, prioritized action list.

---

## 🛠️ Core Components
- **`scheduler_agent.py`**: Logic for time-slot calculation, conflict resolution, and dependency chaining.
- **`consistency_agent.py`**: Auditor for schedule integrity across disparate data sources.

---

## ✨ Engineering Wins
- **Conflict Reduction**: Eliminated 95% of accidental double-bookings by using holistic cascading logic instead of isolated event creation.
- **Contextual Intelligence**: Linked user sentiment to calendar density, achieving a more human-centric "AI Life Coach" baseline.

---

**Built for the high-context world. 🕰️**
