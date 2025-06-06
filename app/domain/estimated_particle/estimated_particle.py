import random
from collections.abc import Iterator
from typing import Literal

import numpy as np

from app.config.const.amount import (
    CLUSTER_AMOUNT_THRESHOLD,
    CONVERGENCE_DECENTRALIZATION_THRESHOLD,
    INITIAL_PARTICLES_AMOUNT,
    MISSING_PARTICLE_THRESHOLD,
    SEARCH_NEAREST_INSIDE_RANGE,
)
from app.config.const.circle import REVERSE_RADIUS
from app.config.const.error import PARTICLES_ANGLE_ERROR, PARTICLES_DIRECTION_ERROR
from app.domain.correct_position.correct_position import CorrectPosition
from app.domain.estimated_particle.convergence_judgment import ConvergenceJudgment
from app.domain.estimated_position.estimated_position import EstimatedPosition
from app.domain.floor_map.floor_map import FloorMap
from app.domain.likelihood.likelihood import Likelihood
from app.domain.particle.particle import Particle
from app.domain.particle_collection.particle_collection import ParticleCollection
from app.utils.angle import get_random_angle


class EstimatedParticle:
    def __init__(
        self,
        floor_map: FloorMap,
        current_position: CorrectPosition,
        particle_collection: ParticleCollection,
    ) -> None:
        particle_collection.shuffle()

        self.__floor_map = floor_map
        self.__missing_particle_count = 0
        self.__current_position = current_position
        self.__particle_collection = particle_collection

    def get_floor_map(self) -> FloorMap:
        return self.__floor_map

    def get_missing_particle_count(self) -> int:
        return self.__missing_particle_count

    def get_current_position(self) -> CorrectPosition:
        return self.__current_position

    def get_particle_collection(self) -> ParticleCollection:
        return self.__particle_collection

    def is_converged(self,clusters_count: int) -> bool:
        """## パーティクルのクラスタ数を計算する"""
        cluster_amount = ConvergenceJudgment.calculate_cluster_amount(self.__particle_collection)

        return (
            cluster_amount <= clusters_count
            and self.get_convergence_ratio() >= CONVERGENCE_DECENTRALIZATION_THRESHOLD
        )

    def get_particles_within_radius(self, x: int, y: int, radius: int) -> "EstimatedParticle":
        """指定された座標を中心とする半径radiusの範囲内に存在するパーティクルを取得する"""
        radius_squared = radius**2

        particles_within_radius = ParticleCollection()
        particles_within_radius.add_all(
            [
                particle
                for particle in self.__particle_collection
                if (particle.get_x() - x) ** 2 + (particle.get_y() - y) ** 2 <= radius_squared
            ]
        )

        return EstimatedParticle(
            floor_map=self.__floor_map,
            current_position=self.__current_position,
            particle_collection=particles_within_radius,
        )

    def get_convergence_ratio(self) -> float:
        """## パーティクルの分散をもとに、収束度を計算する"""
        return 1 / self.__particle_collection.get_decentralization()

    def move(
        self,
        current_position: CorrectPosition,
        particle_step_error_sd: int,
        particle_angle_error_sd: int,
        initial_particle_count: int
    ) -> "EstimatedParticle":
        """## ベクトルの向きに合わせてパーティクルを移動させる"""
        step = current_position.get_step()
        changed_angle = current_position.get_changed_angle()

        move_particle_collection = ParticleCollection()

        moved_particles = [
            particle.move(
                step=step,
                changed_angle=changed_angle,
                step_error=int(random.gauss(0, particle_step_error_sd)),
                angle_error=int(random.gauss(0, particle_angle_error_sd)),
            )
            for particle in self.__particle_collection
        ]

        move_particle_collection.add_all(moved_particles)

        return EstimatedParticle(
            floor_map=self.__floor_map,
            current_position=current_position,
            particle_collection=move_particle_collection,
        )

    def estimate_position(self) -> EstimatedPosition:
        """## 重みづけ平均を元に歩行座標を推定する"""
        estimated_x = self.__particle_collection.get_x_mean()
        estimated_y = self.__particle_collection.get_y_mean()
        estimated_direction = self.__particle_collection.get_direction_mean()

        if not self.__floor_map.is_inside_floor(x=estimated_x, y=estimated_y):
            estimated_x, estimated_y = self.__floor_map.get_nearest_inside_coordinate(
                outside_position=(estimated_x, estimated_y),
                search_range=SEARCH_NEAREST_INSIDE_RANGE,
            )
        return EstimatedPosition(
            x=estimated_x,
            y=estimated_y,
            step=self.__current_position.get_step(),
            direction=estimated_direction,
            changed_angle=self.__current_position.get_changed_angle(),
        )

    def update_weight(self, likelihood: Likelihood, rssi: float) -> "EstimatedParticle":
        """## パーティクルの重みを更新する"""
        self.__particle_collection.set_weights(
            rssi_input=rssi,
            likelihood=likelihood,
        )

        return EstimatedParticle(
            floor_map=self.__floor_map,
            current_position=self.__current_position,
            particle_collection=self.__particle_collection,
        )

    def remove_by_floor_map(self) -> None:
        """## パーティクルが歩行可能領域外に存在する場合、パーティクルを削除する"""
        remove_particle_indexes = [
            i
            for i, particle in enumerate(self.__particle_collection)
            if not self.__floor_map.is_inside_floor(x=particle.get_x(), y=particle.get_y())
        ]

        self.__missing_particle_count += len(remove_particle_indexes)

        self.__particle_collection.pop_all(indexes=remove_particle_indexes)

    def remove_by_direction(self, step: int) -> None:
        """## パーティクルの向きが歩行不可能領域を向いている場合、パーティクルを削除する"""
        remove_particle_indexes = []

        for i, particle in enumerate(self.__particle_collection):
            if particle.is_straight_direction_to_wall(
                step=step, is_inside_floor=self.__floor_map.is_inside_floor
            ):
                remove_particle_indexes.append(i)
                continue

            if particle.is_turn_direction_to_wall(
                step=step,
                is_inside_floor=self.__floor_map.is_inside_floor,
            ):
                remove_particle_indexes.append(i)

        self.__missing_particle_count += len(remove_particle_indexes)
        self.__particle_collection.pop_all(indexes=remove_particle_indexes)

    def resampling(self, step: int, mode: Literal["normal", "reversed"] = "normal") -> None:
        """## リサンプリングを実行する"""
        lost_particle_count = self.__count_lost_particle()
        new_particles: list[Particle] = []

        # 没パーティクルが一定数以下になった場合、新たにパーティクルを生成する
        if lost_particle_count >= MISSING_PARTICLE_THRESHOLD and mode == "normal":
            while lost_particle_count > 0:
                new_particle = Particle.create_random_particle(
                    x_range=self.__floor_map.get_map_width(),
                    y_range=self.__floor_map.get_map_height(),
                )
                new_particles.append(new_particle)
                lost_particle_count -= 1
                if lost_particle_count == 0:
                    break
        else:
            while lost_particle_count > 0:
                for particle in self.__particle_collection:
                    new_particle = particle.new(
                        weight=particle.get_weight(),
                        step=step,
                        direction_error=PARTICLES_ANGLE_ERROR(),
                    )
                    new_particles.append(new_particle)
                    lost_particle_count -= 1
                    if lost_particle_count == 0:
                        break

        self.__particle_collection.add_all(new_particles)

    def resampling_by_weight(self) -> None:
        num_particles = len(self.__particle_collection)
        weights = np.array([p.get_weight() for p in self.__particle_collection])

        # 重みの正規化のロジックここじゃない
        weights /= np.sum(weights)

        # 累積分布関数CDFの計算
        cdf = np.cumsum(weights)

        # リサンプリング位置の決定
        rng = np.random.default_rng()
        positions = (np.arange(num_particles) + rng.uniform(0, 1, num_particles)) / num_particles

        new_particles: list[Particle] = []
        index = 0
        for pos in positions:
            while pos > cdf[index]:
                index += 1
            new_particle = self.__particle_collection[index].new(
                weight=1 / num_particles,
                step=1,
                direction_error=PARTICLES_ANGLE_ERROR(),
            )
            new_particles.append(new_particle)

        self.__particle_collection.reset()
        self.__particle_collection.add_all(new_particles)

    def __count_lost_particle(self) -> int:
        return INITIAL_PARTICLES_AMOUNT - len(self.__particle_collection)

    def __iter__(self) -> Iterator[Particle]:
        return iter(self.__particle_collection)

    def __len__(self) -> int:
        return len(self.__particle_collection)


class EstimatedParticleFactory:
    @staticmethod
    def create(
        floor_map: FloorMap,
        initial_position: CorrectPosition,
        initial_particle_count: int,
    ) -> EstimatedParticle:
        """## 初期パーティクルを散布する"""
        particle_collection = ParticleCollection()
        count = 0

        while len(particle_collection) < initial_particle_count:
            rng = np.random.default_rng()
            # x = rng.integers(floor_map.get_map_width())
            y = rng.integers(floor_map.get_map_height())
            # direction = get_random_angle() # 0 ~ 360の範囲でランダムな角度を生成
 
            x = 350
            # y = 350
            direction = 0
            
            weight = 1 / initial_particle_count

            if not floor_map.is_inside_floor(x=x, y=y):
                continue

            particle_collection.add(
                particle=Particle(
                    x=x,
                    y=y,
                    direction=direction,
                    weight=weight,
                )
            )

        return EstimatedParticle(
            floor_map=floor_map,
            current_position=initial_position,
            particle_collection=particle_collection,
        )

    @staticmethod
    def reverse_create(floor_map: FloorMap, final_position: EstimatedPosition) -> EstimatedParticle:
        """## 逆算軌跡推定の際に行うパーティクルフィルタのパーティクルを散布する"""
        initial_position = CorrectPosition(
            x=final_position.get_x(),
            y=final_position.get_y(),
            step=final_position.get_step(),
            direction=final_position.get_direction(),
            changed_angle=final_position.get_changed_angle(),
            rssi1=0,
            rssi2=0,
        )

        particle_collection = ParticleCollection()

        while len(particle_collection) < INITIAL_PARTICLES_AMOUNT:
            rng = np.random.default_rng()
            x = initial_position.get_x() + rng.integers(-REVERSE_RADIUS, REVERSE_RADIUS)
            y = initial_position.get_y() + rng.integers(-REVERSE_RADIUS, REVERSE_RADIUS)
            direction = initial_position.get_direction() + PARTICLES_DIRECTION_ERROR()
            weight = 1 / INITIAL_PARTICLES_AMOUNT

            if not floor_map.is_inside_floor(x=x, y=y):
                continue

            particle_collection.add(
                particle=Particle(
                    x=x,
                    y=y,
                    direction=direction,
                    weight=weight,
                )
            )

        return EstimatedParticle(
            floor_map=floor_map,
            current_position=initial_position,
            particle_collection=particle_collection,
        )
