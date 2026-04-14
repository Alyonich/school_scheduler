from dataclasses import dataclass, field


@dataclass(frozen=True)
class Placement:
    time_slot_id: int
    classroom_id: int


@dataclass
class Chromosome:
    placements: list[Placement]
    hard_penalty: int = 0
    soft_penalty: int = 0
    score: int = 0
    diagnostics: dict[str, int] = field(default_factory=dict)

    def copy(self) -> 'Chromosome':
        return Chromosome(
            placements=list(self.placements),
            hard_penalty=self.hard_penalty,
            soft_penalty=self.soft_penalty,
            score=self.score,
            diagnostics=dict(self.diagnostics),
        )
