from sqlmodel import Session, select
from sqlalchemy.orm import selectinload
from typing import Dict, Any, Optional, List, cast
from datetime import date as date_type, datetime, timedelta, timezone

from ..models import User, DailySchedule, TimeBlock, LongTermGoal
from .icon_helper import assign_icon

class SchedulerAgent:
    """
    The SchedulerAgent is responsible for creating, managing, and dynamically
    adjusting the user's daily schedule. It acts as the "calendar manager"
    that executes scheduling decisions.
    """
    def __init__(self, session: Session):
        self.session = session

    def get_schedule_for_date(self, user: User, target_date: date_type) -> Optional[DailySchedule]:
        """
        Retrieves the user's schedule for a specific date, eagerly loading time blocks.
        """
        statement = (
            select(DailySchedule)
            .options(
                selectinload(cast(Any, DailySchedule.time_blocks)).selectinload(cast(Any, TimeBlock.related_goal))
            ) # type: ignore
            .where(
                DailySchedule.user_id == user.id,
                DailySchedule.date == target_date
            )
        )
        schedule = self.session.exec(statement).first()
        return schedule

    async def get_or_create_schedule_with_routines(self, user: User, target_date: date_type) -> DailySchedule:
        """
        Ensures a DailySchedule object exists for the given date and is populated with
        active recurring goals (routines).
        Uses smart slotting to avoid overlaps and respect 'Morning/Evening' intent.
        """
        # Ensure we are working with a date object for DB comparison
        if isinstance(target_date, datetime):
            target_date = target_date.date()

        schedule = self.get_schedule_for_date(user, target_date)
        if not schedule:
            schedule = DailySchedule(
                user_id=user.id,
                date=target_date,
                is_locked=False
            )
            self.session.add(schedule)
            self.session.commit()
            # Re-fetch with relationship loaded to ensure clean state
            schedule = self.get_schedule_for_date(user, target_date)
            if not schedule:
                 raise Exception("Failed to create and reload schedule")

        # Now, ensure all active routines or one-time goals due today have a time block
        statement = select(LongTermGoal).where(
            LongTermGoal.user_id == user.id,
            LongTermGoal.status == "in_progress"
        )
        all_goals = self.session.exec(statement).all()

        from .routine_service import RoutineService
        from ..utils.time_utils import get_safe_tz, localize_dt
        routine_service = RoutineService(self.session)

        timezone_name = user.timezone_name or "UTC"
        routines = [g for g in all_goals if routine_service.is_due_on_date(g, target_date, timezone_name=timezone_name)]

        print(f"DEBUG: Found {len(routines)} goals due for user {user.id} on date {target_date}")

        # Check existing time blocks for this schedule to avoid duplicates
        existing_goal_ids = {block.related_goal_id for block in schedule.time_blocks if block.related_goal_id}

        added_any = False
        user_tz = get_safe_tz(timezone_name)
        
        # Helper to detect overlap locally (considering newly added blocks too)
        current_blocks = list(schedule.time_blocks) # Local copy to track new additions
        
        def is_slot_taken(start_dt_utc: datetime, duration_min: int) -> bool:
            end_dt_utc = start_dt_utc + timedelta(minutes=duration_min)
            for block in current_blocks:
                # Ensure existing block time is aware
                if block.start_time.tzinfo is None:
                    block.start_time = block.start_time.replace(tzinfo=timezone.utc)
                if block.end_time.tzinfo is None:
                     block.end_time = block.end_time.replace(tzinfo=timezone.utc)
                     
                if block.start_time < end_dt_utc and block.end_time > start_dt_utc:
                    return True
            return False

        for routine in routines:
            if routine.id not in existing_goal_ids:
                # 1. Determine Base Hour (Local)
                title_lower = routine.title.lower()
                start_h, start_m = 9, 0 # Default 9 AM
                
                # Keyword overrides
                if any(k in title_lower for k in ['sleep', 'evening', 'night', 'bed']):
                    start_h = 21 # 9 PM
                elif any(k in title_lower for k in ['morning', 'wake', 'breakfast']):
                    start_h = 8 # 8 AM
                
                # Duration
                duration = routine.duration_minutes or 60
                
                # 2. Find Smart Slot (Iterate forward)
                final_start_utc = None
                
                # Search window: up to 12 hours forward from base time
                for offset_hours in range(12):
                    candidate_h = start_h + offset_hours
                    if candidate_h >= 24: break
                    
                    # Construct Local Time -> UTC
                    candidate_time =  datetime.strptime(f"{candidate_h:02d}:{start_m:02d}", "%H:%M").time()
                    try:
                        candidate_start_utc = localize_dt(target_date, candidate_time, timezone_name)
                    except Exception as e:
                        print(f"WARN: Failed to localize time {candidate_time}: {e}")
                        continue
                        
                    if not is_slot_taken(candidate_start_utc, duration):
                        final_start_utc = candidate_start_utc
                        break
                
                # Fallback: Just force at base time if no slot found (stacking is better than missing)
                if not final_start_utc:
                    base_time = datetime.strptime(f"{start_h:02d}:{start_m:02d}", "%H:%M").time()
                    final_start_utc = localize_dt(target_date, base_time, timezone_name)

                # Calculate End Time
                final_end_utc = final_start_utc + timedelta(minutes=duration)

                full_title = f"{routine.emoji or '🔄'} {routine.title}"
                import json
                comps = []
                try:
                    comps = json.loads(routine.components_json or "[]")
                except Exception:
                    pass

                # Check for existing counter
                has_counter = any(c.get("type") == "counter" for c in comps)
                if not has_counter:
                    comps.append({"type": "counter", "label": "Streak", "value": routine.streak})

                new_block = TimeBlock(
                    schedule_id=schedule.id,
                    related_goal_id=routine.id,
                    title=full_title,
                    start_time=final_start_utc,
                    end_time=final_end_utc,
                    status="pending",
                    context_note="Automatically added as it is part of your routines.",
                    components_json=json.dumps(comps), # Copy trackers/components + injected counter
                    icon=assign_icon(full_title), # Auto-assign icon (will be None if emoji present)
                    is_fixed=self.determine_is_fixed(full_title, routine.tags or [])
                )
                self.session.add(new_block)
                # Append to local list for next iteration's collision check
                current_blocks.append(new_block)
                added_any = True
            else:
                print(f"DEBUG: Skipping routine {routine.id} as it already exists in schedule.")

        if added_any:
            self.session.commit()
            # Re-fetch with eager loading to ensure related_goal is loaded
            schedule = self.get_schedule_for_date(user, target_date)
            if not schedule:
                raise Exception("Failed to reload schedule after adding routines")

        return schedule

    async def generate_daily_schedule(self, user: User, target_date: date_type) -> DailySchedule:
        """
        Legacy method name, now points to the routine-aware version.
        """
        return await self.get_or_create_schedule_with_routines(user, target_date)

    def reschedule_task(self, task_id: int, new_start_time: str) -> Dict[str, Any]:
        """
        Updates the time of a specific task and handles the consequences.
        """
        # Placeholder for rescheduling logic
        print(f"SchedulerAgent: Rescheduling task {task_id} to {new_start_time} (Not yet implemented)")
        return {"status": "SUCCESS", "impacted_tasks": []}

    def determine_is_fixed(self, title: str, tags: Optional[List[str]] = None) -> bool:
        """
        Heuristic to determine if a task is 'Fixed' (e.g. meeting, appointment)
        based on title and tags.
        """
        fixed_keywords = {
            "meeting", "call", "appointment", "lunch", "dinner", "breakfast",
            "doctor", "interview", "class", "webinar", "flight", "train"
        }

        # Check title
        if any(kw in title.lower() for kw in fixed_keywords):
            return True

        # Check tags
        if tags:
            for tag in tags:
                if any(kw in tag.lower() for kw in fixed_keywords):
                    return True

        return False

    def smart_cascade(self, user: User, completed_block: TimeBlock, actual_completion_time: datetime) -> Dict[str, Any]:
        """
        Triggers a 'Smart Cascade' if a task is completed late.
        Flexible tasks will be shifted, 'jumping over' Fixed tasks.
        """
        # 0. Check User Preference
        if not getattr(user, "auto_cascade_enabled", False):
            return {"status": "SKIPPED", "reason": "Auto-cascade is disabled for this user."}

        # 1. Calculate delay
        if not completed_block.end_time:
            return {"status": "SKIPPED", "reason": "No end time on block"}

        # Ensure UTC comparisons
        if actual_completion_time.tzinfo is None:
            actual_completion_time = actual_completion_time.replace(tzinfo=timezone.utc)

        scheduled_end = completed_block.end_time
        if scheduled_end.tzinfo is None:
            scheduled_end = scheduled_end.replace(tzinfo=timezone.utc)

        # Calculate delay in minutes
        delay_seconds = (actual_completion_time - scheduled_end).total_seconds()
        delay_minutes = int(delay_seconds / 60)

        # Threshold: 15 minutes
        if delay_minutes < 15:
            return {"status": "SKIPPED", "reason": f"Delay {delay_minutes}m within threshold."}

        print(f"DEBUG: Triggering Smart Cascade. Delay: {delay_minutes}m")

        # 2. Fetch subsequent blocks for the day
        schedule = self.get_schedule_for_date(user, completed_block.schedule.date)
        if not schedule:
            return {"status": "ERROR", "reason": "Schedule not found"}

        # Sort by start time. Filter for tasks starting AFTER completion time
        # We only move tasks that haven't started yet or are 'pending'
        # Actually, we should move everything scheduled *after* the original end time of the completed block
        # But if we completed it LATE, we are eating into future blocks.

        pending_blocks = [
            b for b in schedule.time_blocks
            if b.status == "pending"
            and b.id != completed_block.id
            and b.start_time >= scheduled_end # Only move things that were supposed to happen after
        ]

        pending_blocks.sort(key=lambda x: x.start_time)

        if not pending_blocks:
            return {"status": "SKIPPED", "reason": "No subsequent pending blocks."}

        shift_delta = timedelta(minutes=delay_minutes)
        moved_blocks = []

        # 3. Cascade Logic
        # We simulate the timeline forward.
        # 'cursor' is the earliest available time for the *next* flexible task.
        # Initially, it's the actual_completion_time.

        cursor = actual_completion_time

        for block in pending_blocks:
            # Normalize block times for comparison
            if block.start_time.tzinfo is None:
                block.start_time = block.start_time.replace(tzinfo=timezone.utc)
            if block.end_time.tzinfo is None:
                 block.end_time = block.end_time.replace(tzinfo=timezone.utc)

            if block.is_fixed:
                # Fixed tasks stay put.
                # However, we must respect them as barriers.
                # If our cursor (pushed by flexible tasks) has moved PAST the start of this fixed task,
                # we effectively have a conflict that we are ignoring for the fixed task (it stays matches wall clock),
                # but the *next* flexible task must jump over this fixed task.

                # Update cursor to be at least the end of this fixed task,
                # strictly if the previous flow pushed us into it.
                if cursor > block.start_time:
                    # We have "crashed" into this meeting.
                    # The meeting happens. The user attends it.
                    # Our flow of flexible tasks resumes AFTER this meeting.
                    cursor = max(cursor, block.end_time)
                else:
                    # No crash. The meeting happens later.
                    # Does the cursor jump to the meeting end?
                    # No, we might squeeze a task in before the meeting.
                    pass

            else:
                # Flexible Task.
                # It should start at 'cursor' or its original start time + shift?
                # Simplest approach: "Push" logic.
                # New Start = max(Original Start + Shift, cursor)
                # Actually, the 'Shift' is constant from the initial delay.
                # But 'Jump Over' might add MORE delay.

                # Let's use the 'cursor' approach which represents "Next Available Slot".
                # But we shouldn't pull tasks *forward* (earlier) if the delay is small?
                # No, we are ONLY handling Lateness.

                # Original logic: shift by delay.
                proposed_start = block.start_time + shift_delta

                # Check for overlap with any FIXED task
                # We need to check against ALL fixed tasks to find collisions
                # Optimization: checks locally against the list we have

                is_overlap = False
                target_fixed_end = None

                for potential_barrier in pending_blocks:
                    if not potential_barrier.is_fixed:
                        continue

                    # Ensure barrier times are aware
                    pb_start = potential_barrier.start_time.replace(tzinfo=timezone.utc) if potential_barrier.start_time.tzinfo is None else potential_barrier.start_time
                    pb_end = potential_barrier.end_time.replace(tzinfo=timezone.utc) if potential_barrier.end_time.tzinfo is None else potential_barrier.end_time

                    # Check overlap: (StartA < EndB) and (EndA > StartB)
                    # A = Proposed Flexible, B = Fixed Barrier
                    proposed_duration = block.end_time - block.start_time
                    proposed_end = proposed_start + proposed_duration

                    if proposed_start < pb_end and proposed_end > pb_start:
                        # Collision!
                        is_overlap = True
                        target_fixed_end = pb_end
                        # We pick the latest end time if multiple overlaps (rare but possible)
                        # We break? No, might overlap multiple.

                if is_overlap and target_fixed_end:
                    # JUMP OVER
                    # Set start time to the end of the barrier
                    # CRITICAL: Preserve duration by calculating it BEFORE modifying start_time
                    duration = block.end_time - block.start_time
                    
                    # Store original times for undo if not already cascaded in this session
                    # or if we want to allow one-level undo of the *last* cascade.
                    block.cascade_metadata = {
                        "original_start": block.start_time.isoformat(),
                        "original_end": block.end_time.isoformat()
                    }
                    block.was_cascaded = True

                    block.start_time = target_fixed_end
                    block.end_time = block.start_time + duration
                    
                    # Update cursor
                    cursor = block.end_time

                else:
                    # No overlap. Just apply standard shift?
                    # Or use cursor?
                    # If we use cursor, we stack tasks back-to-back.
                    # If we use shift, we preserve gaps.
                    # Preserving gaps is "Smarter".

                    # Use shift, but ensure we don't start before cursor (which ensures sequentiality)
                    proposed_start = max(proposed_start, cursor)
                    duration = block.end_time - block.start_time

                    # Store original times for undo
                    block.cascade_metadata = {
                        "original_start": block.start_time.isoformat(),
                        "original_end": block.end_time.isoformat()
                    }
                    block.was_cascaded = True

                    block.start_time = proposed_start
                    block.end_time = proposed_start + duration
                    cursor = block.end_time

                block.updated_at = datetime.now(timezone.utc)
                self.session.add(block)
                moved_blocks.append(f"{block.title} -> {block.start_time.strftime('%H:%M')}")

        if moved_blocks:
            self.session.commit()
            return {"status": "SUCCESS", "message": f"Cascaded {len(moved_blocks)} tasks.", "details": moved_blocks}

        return {"status": "SKIPPED", "reason": "No tasks needed moving."}
