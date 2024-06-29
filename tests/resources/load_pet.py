import vesin
from metatensor.torch.atomistic import load_atomistic_model, ModelOutput, ModelEvaluationOptions, systems_to_torch
import ase


import random
from typing import List

import ase.neighborlist
import numpy as np
import torch
import vesin
from metatensor.torch import Labels, TensorBlock
from metatensor.torch.atomistic import (
    NeighborListOptions,
    System,
    register_autograd_neighbors,
)

import ase
from metatensor.torch.atomistic import System


def system_to_ase(system: System) -> ase.Atoms:
    """Converts a ``metatensor.torch.atomistic.System`` to an ``ase.Atoms`` object.
    This will discard any neighbor lists attached to the ``System``.

    :param system: The system to convert.

    :return: The system as an ``ase.Atoms`` object.
    """

    # Convert the system to an ASE atoms object
    positions = system.positions.detach().cpu().numpy()
    numbers = system.types.detach().cpu().numpy()
    cell = system.cell.detach().cpu().numpy()
    pbc = list(cell.any(axis=1))
    atoms = ase.Atoms(
        numbers=numbers,
        positions=positions,
        cell=cell,
        pbc=pbc,
    )

    return atoms



def get_system_with_neighbor_lists(
    system: System, neighbor_lists: List[NeighborListOptions]
) -> System:
    """Attaches neighbor lists to a `System` object.

    :param system: The system for which to calculate neighbor lists.
    :param neighbor_lists: A list of `NeighborListOptions` objects,
        each of which specifies the parameters for a neighbor list.

    :return: The `System` object with the neighbor lists added.
    """
    # Convert the system to an ASE atoms object
    atoms = system_to_ase(system)

    # Compute the neighbor lists
    for options in neighbor_lists:
        if options not in system.known_neighbor_lists():
            neighbor_list = _compute_single_neighbor_list(atoms, options).to(
                device=system.device, dtype=system.dtype
            )
            register_autograd_neighbors(system, neighbor_list)
            system.add_neighbor_list(options, neighbor_list)

    return system


def _compute_single_neighbor_list(
    atoms: ase.Atoms, options: NeighborListOptions
) -> TensorBlock:
    # Computes a single neighbor list for an ASE atoms object
    # (as in metatensor.torch.atomistic)

    if np.all(atoms.pbc) or np.all(~atoms.pbc):
        nl_i, nl_j, nl_S, nl_D = vesin.ase_neighbor_list(
            "ijSD",
            atoms,
            cutoff=options.cutoff,
        )
    else:
        # this is not implemented in vesin, so we use ASE
        nl_i, nl_j, nl_S, nl_D = ase.neighborlist.neighbor_list(
            "ijSD",
            atoms,
            cutoff=options.cutoff,
        )

    # Check the vesin NL against the ASE NL (5% of the time)
    if random.random() < 0.05:
        nl_i_ase, nl_j_ase, nl_S_ase, nl_D_ase = ase.neighborlist.neighbor_list(
            "ijSD",
            atoms,
            cutoff=options.cutoff,
        )
        assert len(nl_i) == len(nl_i_ase)
        assert len(nl_j) == len(nl_j_ase)
        assert len(nl_S) == len(nl_S_ase)
        assert len(nl_D) == len(nl_D_ase)
        nl_ijS = np.concatenate(
            (nl_i.reshape(-1, 1), nl_j.reshape(-1, 1), nl_S), axis=1
        )
        nl_ijS_ase = np.concatenate(
            (nl_i_ase.reshape(-1, 1), nl_j_ase.reshape(-1, 1), nl_S_ase), axis=1
        )
        sort_indices = np.lexsort(nl_ijS.T)
        sort_indices_ase = np.lexsort(nl_ijS_ase.T)
        assert np.array_equal(nl_ijS[sort_indices], nl_ijS_ase[sort_indices_ase])
        assert np.allclose(nl_D[sort_indices], nl_D_ase[sort_indices_ase])

    selected = []
    for pair_i, (i, j, S) in enumerate(zip(nl_i, nl_j, nl_S)):
        # we want a half neighbor list, so drop all duplicated neighbors
        if j < i:
            continue
        elif i == j:
            if S[0] == 0 and S[1] == 0 and S[2] == 0:
                # only create pairs with the same atom twice if the pair spans more
                # than one unit cell
                continue
            elif S[0] + S[1] + S[2] < 0 or (
                (S[0] + S[1] + S[2] == 0) and (S[2] < 0 or (S[2] == 0 and S[1] < 0))
            ):
                # When creating pairs between an atom and one of its periodic
                # images, the code generate multiple redundant pairs (e.g. with
                # shifts 0 1 1 and 0 -1 -1); and we want to only keep one of these.
                # We keep the pair in the positive half plane of shifts.
                continue

        selected.append(pair_i)

    selected = np.array(selected, dtype=np.int32)
    n_pairs = len(selected)

    if options.full_list:
        distances = np.empty((2 * n_pairs, 3), dtype=np.float64)
        samples = np.empty((2 * n_pairs, 5), dtype=np.int32)
    else:
        distances = np.empty((n_pairs, 3), dtype=np.float64)
        samples = np.empty((n_pairs, 5), dtype=np.int32)

    samples[:n_pairs, 0] = nl_i[selected]
    samples[:n_pairs, 1] = nl_j[selected]
    samples[:n_pairs, 2:] = nl_S[selected]

    distances[:n_pairs] = nl_D[selected]

    if options.full_list:
        samples[n_pairs:, 0] = nl_j[selected]
        samples[n_pairs:, 1] = nl_i[selected]
        samples[n_pairs:, 2:] = -nl_S[selected]

        distances[n_pairs:] = -nl_D[selected]

    distances = torch.from_numpy(distances)
    return TensorBlock(
        values=distances.reshape(-1, 3, 1),
        samples=Labels(
            names=[
                "first_atom",
                "second_atom",
                "cell_shift_a",
                "cell_shift_b",
                "cell_shift_c",
            ],
            values=torch.from_numpy(samples),
        ),
        components=[Labels.range("xyz", 3)],
        properties=Labels.range("distance", 1),
    )



model = load_atomistic_model("model.pt")
model = model.to("cuda")

evaluation_options = ModelEvaluationOptions(
    length_unit = "angstrom",
    outputs={"mtt::aux::last_layer_features": ModelOutput()}
)

atoms = ase.Atoms("H2O", positions=[[0, 0, 0], [0, 0, 1], [0, 1, 0]])
system = systems_to_torch(atoms)
requested_nls = model.requested_neighbor_lists()
system = get_system_with_neighbor_lists(system, requested_nls)
system = system.to("cuda")

output = model([system, system, system, system], evaluation_options, check_consistency=True)
print(output["mtt::aux::last_layer_features"].block().values.shape)



