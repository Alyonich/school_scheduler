# School Scheduler

Django diploma project for editing and generating school timetables with a genetic mutation algorithm.

## What is included

- Weekly timetable generation for multiple classes.
- Hard constraint handling for class, teacher, and classroom collisions.
- Teacher availability support.
- Subject distribution limits such as "no more than 2 lessons per day".
- Parallel classes and grade-level aware data model.
- Manual timetable editing with lockable lessons that stay fixed on regeneration.
- Demo-ready dashboard, weekly timetable view, admin panel, and realistic seed data.

## Main pages

- `/` dashboard with statistics and generation controls.
- `/timetable/` weekly timetable viewer and editor.
- `/admin/` Django admin for full data inspection.

## Setup

1. Install dependencies in your environment.
2. Run migrations:

```bash
python manage.py migrate
```

3. Seed realistic demo data and generate a sample week:

```bash
python manage.py seed_demo_data
```

4. Start the server:

```bash
python manage.py runserver
```

## Demo credentials

- Admin username: `admin`
- Admin password: `admin12345`

## Data model highlights

- `Class` stores grade, parallel, level, and class size.
- `Subject` stores required room type and daily lesson limit.
- `Teacher` stores weekly and daily workload limits.
- `TeachingAssignment` links teachers to class-subject combinations.
- `TeacherAvailability` marks blocked time slots.
- `Schedule` stores generated or manually edited lessons and supports locking.

## Generator notes

The generator creates lesson requirements from teaching assignments and class subjects, then evolves timetable candidates through:

- heuristic population initialization
- crossover between chromosomes
- mutation of time slot and room assignments
- weighted fitness scoring
- deterministic repair before saving

This combination makes the demo robust enough for presentation while still reflecting a genuine evolutionary scheduling approach.
