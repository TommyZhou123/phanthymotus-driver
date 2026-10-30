# TRON2 VOP follow field compatibility update

This delivery branch does not change PR464 or create another PR.
It deploys the tested follow modules from PR464 commitadfee05 into an exact reviewed
0.11.0-grasp-close-local1 base, retaining teaching, capture/replay SDK integration,
local grasp semantics, config, home, existing mounts and image defaults.

The script adds one inert follow card on the original15791 service. Only camera-to-VOP is wired; the follow card has no canvas ports and internally subscribes to the fixed head VOP topic. The already-built65481a3 image provides unchanged modules; the two updated follow modules and tests come from the pinned application commit with SHA256 checks. It stages first:
source/identity/idle checks, extract/hash PR modules without starting the PR image,
network-disabled COPY-only build and50 isolated synthetic integration tests. Apply
revalidates the runtime, creates a stopped candidate, then switches only TRON2.
Original container/image remain for rollback. No robot commands or shared platform
file edits are implemented. Normal Core re-registration updates tool discovery.

Local validation:50 compatibility integration tests and20 deployment checks passed;
actual ARM64 staging/switch requires the operator's machine. The current Perception
VOP version, actual DDS flow and physical follow/home still need field acceptance.
Successful deployment remains idle; project start never enables follow.

Run `sudo python3 follow_local.py --stage`, then after checking stationary robot and
empty jaws `sudo python3 follow_local.py --apply --confirm-empty-stationary`.
The private persistent journal is under
`/opt/phanthy-motus/projects/tron2/deployment/vop-follow-adfee05`.
`--status` reads state. Once staged, the helper is retained there as`driver-update.py`;
`--rollback --confirm-empty-stationary` restores the original retained container.
Do not repeat unknown mutations. This is a local runtime override; a later platform
image redeployment can replace it. No image/volume pruning or other-service cleanup.
