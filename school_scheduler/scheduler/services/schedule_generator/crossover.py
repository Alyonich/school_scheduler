import random

from .chromosome import Chromosome, Placement


def crossover(parent_a: Chromosome, parent_b: Chromosome, randomizer: random.Random) -> Chromosome:
    placements: list[Placement] = []
    for placement_a, placement_b in zip(parent_a.placements, parent_b.placements):
        placements.append(placement_a if randomizer.random() < 0.5 else placement_b)
    return Chromosome(placements=placements)
