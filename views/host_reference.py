import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

render_host(
    [
        ("Architecture", "10_Architecture.py"),
        ("Signal Lifecycle", "11_Signal_Lifecycle.py"),
        ("RAG Inventory", "14_RAG_Inventory.py"),
        # Library surface (config#2588): browsable private-docs system-doc
        # corpus. SYSTEM_STATE.md/system_state/*.md, ARCHITECTURE.md,
        # EXPERIMENTS.md, STATUS_GENERATED.md read straight off disk via
        # loaders/system_docs_loader.py (same 4-tier boot-pull path
        # resolution as loaders/observation_registry_loader.py). Registries
        # (ARTIFACT_REGISTRY.yaml / OBSERVATION_REGISTRY.yaml) are NOT
        # duplicated here — System State cross-links their existing
        # Observability tabs instead. Pipeline-diagrams tab intentionally
        # NOT added yet — gated on alpha-engine-config-I2587 shipping
        # PIPELINE_DIAGRAMS_GENERATED.md; fast-follow once it does.
        ("System State", "50_System_State.py"),
        ("Architecture Doc", "51_Architecture_Doc.py"),
        ("Experiments Log", "52_Experiments_Log.py"),
        ("Generated Status", "53_Status_Generated.py"),
    ],
    key="host_reference",
)
