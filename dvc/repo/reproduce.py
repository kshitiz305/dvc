from __future__ import unicode_literals

import os
import logging

from dvc.exceptions import ReproductionError
from dvc.repo.scm_context import scm_context
from dvc.utils import relpath

logger = logging.getLogger(__name__)


def _reproduce_stage(stages, node, **kwargs):
    stage = stages[node]

    if stage.locked:
        logger.warning(
            "DVC-file '{path}' is locked. Its dependencies are"
            " not going to be reproduced.".format(path=stage.relpath)
        )

    stage = stage.reproduce(**kwargs)
    if not stage:
        return []

    if not kwargs.get("dry", False):
        stage.dump()

    return [stage]


@scm_context
def reproduce(
    self,
    target=None,
    recursive=False,
    pipeline=False,
    all_pipelines=False,
    **kwargs
):
    import networkx as nx
    from dvc.stage import Stage

    if not target and not all_pipelines:
        raise ValueError()

    interactive = kwargs.get("interactive", False)
    if not interactive:
        config = self.config
        core = config.config[config.SECTION_CORE]
        kwargs["interactive"] = core.get(
            config.SECTION_CORE_INTERACTIVE, False
        )

    targets = []
    if recursive and os.path.isdir(target):
        G = self.graph(from_directory=target)[1]
        dir_targets = [
            os.path.join(self.root_dir, n) for n in nx.dfs_postorder_nodes(G)
        ]
        targets.extend(dir_targets)
    elif pipeline or all_pipelines:
        if pipeline:
            stage = Stage.load(self, target)
            node = relpath(stage.path, self.root_dir)
            pipelines = [self._get_pipeline(node)]
        else:
            pipelines = self.pipelines()

        for G in pipelines:
            for node in G.nodes():
                if G.in_degree(node) == 0:
                    targets.append(os.path.join(self.root_dir, node))
    else:
        targets.append(target)

    ret = []
    with self.state:
        for target in targets:
            stages = _reproduce(self, target, **kwargs)
            ret.extend(stages)

    return ret


def _reproduce(self, target, single_item=False, **kwargs):
    import networkx as nx
    from dvc.stage import Stage

    stage = Stage.load(self, target)
    G = self.graph()[1]
    stages = nx.get_node_attributes(G, "stage")
    node = relpath(stage.path, self.root_dir)

    if single_item:
        ret = _reproduce_stage(stages, node, **kwargs)
    else:
        ret = _reproduce_stages(G, stages, node, **kwargs)

    return ret


def _reproduce_stages(
    G, stages, node, downstream=False, ignore_build_cache=False, **kwargs
):
    r"""Derive the evaluation of the given node for the given graph.

    When you _reproduce a stage_, you want to _evaluate the descendants_
    to know if it make sense to _recompute_ it. A post-ordered search
    will give us an order list of the nodes we want.

    For example, let's say that we have the following pipeline:

                               E
                              / \
                             D   F
                            / \   \
                           B   C   G
                            \ /
                             A

    The derived evaluation of D would be: [A, B, C, D]

    In case that `downstream` option is specifed, the desired effect
    is to derive the evaluation starting from the given stage up to the
    ancestors. However, the `networkx.ancestors` returns a set, without
    any guarantee of any order, so we are going to reverse the graph and
    use a pre-ordered search using the given stage as a starting point.

                   E                                   A
                  / \                                 / \
                 D   F                               B   C   G
                / \   \        --- reverse -->        \ /   /
               B   C   G                               D   F
                \ /                                     \ /
                 A                                       E

    The derived evaluation of _downstream_ B would be: [B, D, E]
    """

    import networkx as nx

    if downstream:
        # NOTE (py3 only):
        # Python's `deepcopy` defaults to pickle/unpickle the object.
        # Stages are complex objects (with references to `repo`, `outs`,
        # and `deps`) that cause struggles when you try to serialize them.
        # We need to create a copy of the graph itself, and then reverse it,
        # instead of using graph.reverse() directly because it calls
        # `deepcopy` underneath -- unless copy=False is specified.
        pipeline = nx.dfs_preorder_nodes(G.copy().reverse(copy=False), node)
    else:
        pipeline = nx.dfs_postorder_nodes(G, node)

    result = []
    for n in pipeline:
        try:
            ret = _reproduce_stage(stages, n, **kwargs)

            if len(ret) != 0 and ignore_build_cache:
                # NOTE: we are walking our pipeline from the top to the
                # bottom. If one stage is changed, it will be reproduced,
                # which tells us that we should force reproducing all of
                # the other stages down below, even if their direct
                # dependencies didn't change.
                kwargs["force"] = True

            result += ret
        except Exception as ex:
            raise ReproductionError(stages[n].relpath, ex)
    return result
