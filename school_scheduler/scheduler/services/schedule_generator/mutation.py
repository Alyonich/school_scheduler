import random

from .chromosome import Chromosome, Placement
from .data_loader import GenerationContext


def mutate(
    chromosome: Chromosome,
    context: GenerationContext,
    mutation_rate: float,
    randomizer: random.Random,
    room_choices: dict[str, list[int]],
) -> Chromosome:
    mutated = chromosome.copy()
    slot_ids = [slot.id for slot in context.time_slots]

    for index, requirement in enumerate(context.lesson_requirements):
        if randomizer.random() > mutation_rate:
            continue

        placement = mutated.placements[index]
        candidate_slots = slot_ids[:]
        randomizer.shuffle(candidate_slots)
        candidate_rooms = [
            room_id
            for room_id in room_choices.get(requirement.required_room_type, [])
            if context.classrooms[room_id].capacity >= requirement.min_capacity
        ]
        if not candidate_rooms:
            candidate_rooms = [
                room_id
                for room_id, room in context.classrooms.items()
                if room.capacity >= requirement.min_capacity
            ] or list(context.classrooms.keys())
        candidate_rooms = candidate_rooms[:]
        randomizer.shuffle(candidate_rooms)

        if randomizer.random() < 0.6 and candidate_slots:
            placement = Placement(
                time_slot_id=candidate_slots[0],
                classroom_id=placement.classroom_id,
            )
        if candidate_rooms:
            placement = Placement(
                time_slot_id=placement.time_slot_id,
                classroom_id=candidate_rooms[0],
            )
        mutated.placements[index] = placement

    return mutated
