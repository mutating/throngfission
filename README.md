# throngfission

A [throng](https://github.com/mutating/throng) plugin for running commands in
[Fission](https://fission.io/) on Kubernetes. Each isolate gets its own pod,
workspace and Python virtual environment. Files and installed packages persist
between commands until the isolate is destroyed.

## Installation and use

```sh
python -m venv .venv
.venv/bin/python -m pip install throngfission
```

Prepare a Linux K3s cluster (Kubernetes 1.36 or newer) once:

```sh
.venv/bin/throngfission setup
# Equivalent: .venv/bin/python -m throngfission setup
```

The command uses the current kubeconfig/context. It requires cluster-admin access,
`kubectl`, Bash and, when creating certificates, `openssl`. Building or loading
missing runtime images also needs Docker with buildx and `curl`. Helm and
Autofission are not required and are not installed. The isolate allocation
mechanism remains Fission `poolmgr`.

For an external build disk or a different context:

```sh
.venv/bin/throngfission setup --context compute --work-dir /Volumes/CodexHome
```

`--work-dir` must already exist. It holds temporary source, certificate and image
archive files; they are removed after each operation. Docker stores images and
build cache on its own configured disk, independently of this directory.

Setup checks cluster access and Ready nodes, installs the pinned Fission 1.27.0
manifests if needed, prepares each missing image on each Ready node, installs the
UID-remapping admission policy, and creates three shared functions. The same
worker code is used in `throngfission-python`, `throngfission-debian` and
`throngfission-ubuntu`; each function references its own runtime image. A
first-use diagnostic checks root inside a user namespace, child-process
termination, shell and pip. The diagnostic isolate is then deleted.
If its connection is lost during allocation, setup acquires a new diagnostic
worker within the same `diagnostic_timeout`. Once diagnostic commands start,
they are not replayed automatically.

All manifest resources are read before creation. Existing external Fission
installations are retained; their function namespace and WebSocket capabilities
must already support the selected plugin settings. For installations created by
setup, missing Fission components are repaired individually. Setup never upgrades
or replaces an existing installation in place.

Runtime inventory is checked separately for each node. Complete nodes are skipped;
new nodes receive their missing images on the next invocation. Offline nodes are
reported and skipped, including their image checks; rerun setup after they return.
Imports are verified through containerd/CRI and pinned against kubelet image GC.
Temporary image-import Jobs access the node's containerd socket and use an
unconfined AppArmor context; these administrative permissions are not granted
to isolate workers.
Ready-node image inventory is supplied by kubelet and can lag behind manual cache
changes. Keep the kubelet image inventory limit large enough to include the three
preset images. Setup does not remove old image versions automatically.

Successful diagnostics are recorded with the identities of the shared resources.
If setup is interrupted, run the same command again: existing resources are kept,
missing resources are created, and an unfinished diagnostic is retried. Replacing
a shared resource invalidates its diagnostic checkpoint. A completely prepared
cluster needs only reads and admission dry-runs; setup creates no new workers,
Jobs or image archives in that case. Temporary image-import Jobs have deadlines
and cleanup TTLs even if the local process is terminated abruptly.

Setup generates cluster-specific authentication secrets and webhook TLS keys;
the public credentials embedded in Fission's downloadable manifest are not used.
Its bundled JSON is derived from the official 1.27.0 release manifest and CRDs,
with namespace placeholders and without Helm hook Jobs (telemetry and pre-upgrade
checks). Fission's Apache 2.0 license is included. The same setup path is used by
the disposable three-node e2e fixture.

The default runtime is a pinned multi-platform Fission Python
image with Python 3.13 on Alpine Linux (musl). Local clients support Python 3.8 and newer. The plugin uses
the official Kubernetes Python client to load kubeconfig and manage Fission's
custom resources; Fission does not provide an official Python SDK.

Dependencies must support the runtime's operating system and CPU architecture.
For example, Playwright requires a compatible glibc-based Linux distribution;
it cannot be installed into the default Alpine environment. Use the `image`
setting for a compatible Fission runtime with the required system dependencies.
Installing Python packages does not install system tools such as Hugo or browsers.

[pristan](https://github.com/mutating/pristan) discovers the installed entry point:

```python
from throng import throng

manager = throng('./project', exclude=['.git', '.venv'])['fission']
with manager.scope as isolate:
    isolate.install('pytest')
    result = isolate.run('python -m pytest')
    print(result.stdout, result.stderr)
    snapshot = isolate.read()
```

Commands are split into arguments without an implicit shell. Use `sh -c '...'`
for shell expansion, pipes or redirects. `install()` invokes pip in the isolate's
private virtual environment; it does not install anything automatically for a
particular command. Docker inside an isolate is not supported.

The plugin requires throng 0.0.9 or newer. Configure packages and preparation
commands when creating a manager to run them in **every new isolate**:

```python
from cantok import TimeoutToken
from throng import throng

manager = throng(
    './project',
    exclude=['.git', '.venv'],
    packages=['pytest'],
    prepare=['python -m compileall -q .'],
)['fission']
result = manager.run('python -m pytest', token=TimeoutToken(300), exception=True)
```

`FissionManager` accepts the same `prepare` and `packages` arguments. Each
`get()`, scope entry, `run()` or `chain()` first uploads the workspace, installs
the packages together with pip, then runs preparation commands in order. Packages
are installed before `prepare`, so system dependencies needed by pip must already
be available in the runtime image. Preparation does not run again for subsequent
commands on the same retained isolate.

The caller's token is shared by allocation, upload, package installation,
preparation and user commands in `manager.run()` and `manager.chain()`. To choose
deadlines for a retained isolate, use `with manager.scope(token=token) as isolate`
and pass a token explicitly to commands inside the block. The scope token applies
to creation and preparation; it does not become an implicit command token.

`run()` and `chain()` accept `exception=True`, an exception class, or an exception
instance. `True` raises `NotSuccessfulRunError` for an unsuccessful command;
its `result` retains the return code, stdout and stderr. A cancelled chain raises
`InterruptedChainError` before the next command. With the default
`exception=False`, unsuccessful results are returned and the chain continues.
An isolate remains usable after a command failure; manager convenience calls
always destroy their isolate afterwards.

Installation or preparation failures prevent user commands and destroy the new
isolate, even with `exception=False`. They raise `InterruptedInstallationError`
or `PreparationCommandFailedError`, with the original error in `__cause__`.
Direct `isolate.install(..., token=token)` raises `InterruptedInstallationError`
if cancelled before starting pip, or `CannotInstallDependencyError` with a
`result` if pip exits unsuccessfully or is cancelled while running. Direct
installation leaves the isolate available to its caller. Exceptions are defined
in `throng.errors`; `ThrongError` also catches the plugin's `FissionError` family.

Worker commands run as root **inside a pod user namespace** (`hostUsers: false`).
Kubernetes maps that UID/GID 0 to non-root IDs on the node. Workers do not use
privileged mode or allow privilege escalation, and use the runtime's default
seccomp profile. The fetcher keeps its Fission identity inside the same user
namespace; cleanup Jobs use their own user namespace and a non-root UID 10001
with all capabilities dropped. The client checks the actual worker pod's
`hostUsers` value before accepting a session and deletes workers without the
required user namespace.
This requires Kubernetes, its container runtime and the node filesystems to
support pod user namespaces; there is no fallback to root in the host user
namespace. See [Kubernetes user namespace requirements](https://kubernetes.io/docs/concepts/workloads/pods/user-namespaces/).

Fission 1.27 drops `hostUsers` when merging the Environment's pod specification
into its Deployment. `throngfission setup` installs the admission-policy
workaround before creating workers and verifies it with a dry-run pod.

The generated MutatingAdmissionPolicy and its binding set `hostUsers: false`
only on newly created pods labelled `throngfission.io/installation`. They do not
change other Fission functions or existing pods. This shared policy outlives
individual installations; `manager.uninstall()` does not remove it. Configure
it before allowing workers to start as root, and keep it installed while using
the plugin. Before creating any installation resources, the client submits a
pod with `dryRun=All` and verifies that admission enables UID remapping. This
requires permission to create pods in the installation namespace, but does not
create a real pod. Missing policy or permission causes allocation to fail before
root workers start. The client also rejects and deletes a worker if UID remapping is
absent in its actual pod specification. The disposable CI cluster installs the
same policy in its e2e fixture.

On affected Ubuntu kernels, containerd's default AppArmor profile can reject
signals between a process and its own children when their profiles are stacked
([containerd issue 12886](https://github.com/containerd/containerd/issues/12886)).
This also breaks cancellation and application cleanup. Setup automatically selects
the dedicated `throngfission-stacked-v1` profile when it detects Ubuntu 26.04 nodes.
It installs the profile only on nodes without its preparation checkpoint. Set
`THRONGFISSION_APPARMOR_PROFILE` to an empty string to use the runtime default, or
to an already installed custom profile name. The saved
`throngfission/cli/assets/install_apparmor.sh` is also available for explicit administration.

The installer retains containerd 2.3.2's default deny rules and adds signal and
ptrace peer matches for stacked versions of its own profile. It installs the
separate `throngfission-stacked-v1` profile on Ready K3s nodes with AppArmor and
`apparmor_parser`; other workloads keep their existing profiles. Temporary
administrator Jobs load the kernel profile with `MAC_ADMIN`, an unconfined
AppArmor context and access to the host's profile directory. These privileges
belong only to the installer and are not given to Fission workers. The Jobs carry
their own profile data and expire even after client failure; the installed
profile intentionally persists in `/etc/apparmor.d`.
Rerun setup when an offline or new node joins. Setup installs this profile on
every Ready node before selecting it in the admission policy. Workers
keep AppArmor enforcement, seccomp and UID remapping. The workaround is optional
and is not needed on runtimes without this AppArmor defect.

Commands can install OS packages into their container, for example
`isolate.run('sh -c "apt-get update && apt-get install -y git"')` with a Debian
runtime. Those changes survive commands in that isolate, are removed with its
pod, and are not included in workspace snapshots. Preinstalled system packages
in a custom runtime image avoid repeating downloads for every isolate.
Existing installations require a new name or explicit uninstall to recreate
their pods with the new security settings. User namespaces strengthen process
isolation but still share the node kernel; they are not a virtual machine.

`manager.read()` and `isolate.read()` return TAR bytes compatible with throng's
`temporary_directory` plugin. Pass those bytes to `manager.get(snapshot)` to
restore a workspace in a new isolate. Installed packages, the pip cache and
command output files are outside the workspace and are not included in snapshots.

### Selecting a runtime image

Set `THRONGFISSION_IMAGE` when using the plugin through throng or throngtest.
The value is read through skelet when the manager is constructed. Three short
identifiers select compatible Fission runtimes:

| Identifier | Operating system | Python |
| --- | --- | --- |
| `python` (default) | Alpine, official Fission runtime | 3.13 |
| `debian` | Debian 12 (Bookworm) | 3.13 |
| `ubuntu` | Ubuntu 24.04 LTS (Noble) | 3.12 |

These identifiers resolve to versioned runtime images, not to the plain Docker
Hub images named `python`, `debian` or `ubuntu`. Ubuntu uses the distribution's
[Python 3.12 package](https://packages.ubuntu.com/noble/python3.12). Each preset
includes Python, pip, shell and the Fission server. Project dependencies, browsers
and tools such as Hugo must still be installed explicitly.

Setup prepares all three images without a registry. The saved script remains
available for explicit image administration:

```sh
bash throngfission/cli/assets/build_image.sh --all --work-dir /Volumes/CodexHome
```

The script downloads the pinned official Python runtime, builds Debian and Ubuntu
from pinned base images and Fission sources, then imports and verifies all three
on each Ready K3s node. Docker keeps a local copy of each image. Existing tags are
reused; temporary archives and import Jobs are removed. The example work directory
must exist; choose a directory on a disk with enough space. Docker's image store
and build cache use the Docker daemon's configured disk, independently of this
directory. Imports are pinned against kubelet image garbage collection; old
versions must be removed explicitly when no longer needed.

Use `--image ubuntu` (or `debian`, `python`) to prepare just one image,
`--build-only` to keep it locally without accessing Kubernetes, and
`--platform linux/amd64` for x86 nodes (the default is `linux/arm64`). Offline
nodes are skipped. When one returns, load its missing images with, for example:

```sh
bash throngfission/cli/assets/build_image.sh --all --load-only --nodes pi-4 --work-dir /Volumes/CodexHome
```

From a project's directory with its virtual environment in `venv`:

```sh
THRONGFISSION_IMAGE=ubuntu \
venv/bin/python -m pytest --throngtest-backend=fission --throngtest-python=python
```

Replace `ubuntu` with `debian` or `python` to select another preset. Full custom
references such as `registry.example.com/team/fission-python:3.13` or digest
references are also accepted unchanged. All eligible worker nodes must have the
selected image locally or be able to pull it. The Raspberry Pi cluster needs
Linux ARM64 images. Both workers and cleanup Jobs use the selected image.

This selects an existing image, without building one or installing a Fission
server into it. The image must provide the Fission Python runtime with GEVENT
WebSocket support, its specialization and health endpoints, and `python`, `venv`
and `pip`. A plain `python:3.13-slim` or Playwright image is not sufficient by
itself. For browser tests, prepare a compatible runtime based on Debian/Ubuntu
with the required browsers and system tools. Python test dependencies are still
installed explicitly through the runner's preparation commands or `isolate.install()`.

Selecting another image automatically selects its function; no `NAME` variable
is needed. Full references to preset images select the same functions as their
short identifiers. Other custom image references receive deterministic names
based on the image reference, and their shared resources are created on first use.
Setup prepares only the three built-in presets.

The same setting is available directly in Python:

```python
from throngfission import FissionManager, Settings

manager = FissionManager('./project', settings=Settings(
    image='debian',
))
```

Explicit `Settings(...)` replace environment-derived configuration for that
manager. Changing the client's local Python environment does not change the
Python version or operating system inside an isolate.

For explicit configuration and cancellation:

```python
from cantok import TimeoutToken
from throngfission import FissionManager, Settings

manager = FissionManager('./project', settings=Settings(
    namespace='fission-function',
    cpu_request='500m',
    memory_request='512Mi',
))
isolate = manager.get(manager.read(), token=TimeoutToken(300))
try:
    result = isolate.run('python script.py', token=TimeoutToken(3600))
finally:
    isolate.kill()
```

Cancellation during allocation raises cantok's cancellation exception. With the
default `exception=False`, cancellation of a running command kills its process
group and returns a result with
`killed_by_token=True`; the isolate remains usable. Background descendants in the
command's process group are also terminated when that command finishes. Output is returned when the
command finishes, with stdout and stderr kept separately.

## Resource allocation

Each image shares one Environment, Function, Package and HTTPTrigger. Setup
creates three such installations; clients choosing the same image reuse them.
Fission's `poolmgr` executor uses `onceOnly` specialization: every accepted
WebSocket session gets a distinct pod, which is never reused for another isolate.
The archive is uploaded once per isolate. Commands then travel over that session.

Allocation is optimistic. Kubernetes schedules pods according to their CPU and
memory **requests**. When resources are occupied, the replacement warm-pool pods
remain Pending and `get()` waits for capacity. There is no plugin concurrency cap,
ResourceQuota or mandatory allocation deadline. Supply a cancellation token when
the caller needs a deadline. API validation and permission failures are reported,
as are pod states `InvalidImageName` and `CreateContainerConfigError`.
Retryable router/network failures and other unready-pool states continue waiting;
this includes some persistent problems, such as an unreachable router or an image
that cannot be pulled. Waiting alone does not diagnose those failures. Use a
cancellation token and inspect Fission events/logs when allocation stalls.

Fission only assigns ready pool pods, so `get()` returns after the worker has
initialized its workspace. Pending pods belong to the shared pool, rather than to
returned isolate objects. `pool_size` controls the number of spare pods, not the
maximum number of active isolates. It applies separately to each image: setup
with the default `pool_size=2` keeps six spare pods across the three presets.
Those pods consume scheduling requests even when their image is unused.
Concurrent creation and pool replenishment can temporarily leave additional pods Pending.

Choose requests for the expected workload, including its child processes. The
default `250m` CPU request is a scheduling reservation, not a CPU usage limit.
Several CPU-heavy isolates can otherwise share one node and contend there while
other nodes have idle CPU: Kubernetes places pods from their requests, not their
eventual measured consumption. Application timeouts still apply under that load.

Worker placement is controlled by `anti_affinity` (`THRONGFISSION_ANTI_AFFINITY`):

| Mode | Placement | Capacity behavior |
| --- | --- | --- |
| `none` | No plugin anti-affinity; Kubernetes uses its other scheduling rules | Several workers may share a node while another is idle |
| `soft` (default) | Prefer nodes without another worker of the same function, with scheduler weight 100 | Can place multiple workers on one node when other scheduling scores or capacity require it |
| `hard` | Require a node without another worker of the same function | At most one worker of that function per eligible node; additional workers remain Pending until a node becomes free |

The rule matches Fission workers by `environmentName` within the function
namespace. Warm-pool pods count too; janitor Jobs and workers of the other image
presets do not. Neither mode evicts or moves pods that were already scheduled.
With three Ready nodes, `hard` can therefore run at most three isolates of a
given function at once, regardless of spare CPU or memory. `soft` keeps the
plugin's wait-for-capacity behavior without imposing that limit. It improves
the odds of using an otherwise idle node, but cannot guarantee a balanced
distribution or a particular test duration. See Kubernetes' [pod anti-affinity
rules](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/#inter-pod-affinity-and-anti-affinity)
for the scheduler semantics.

Choose the same mode for setup and every client using a shared function. For
example, to switch the three preset functions to hard placement after their
active isolates have finished:

```sh
THRONGFISSION_ANTI_AFFINITY=hard throngfission setup
THRONGFISSION_ANTI_AFFINITY=hard pytest --throngtest-backend=fission
```

Use `soft` or `none` in both commands to select another mode. `setup` replaces
the affected idle function installations and diagnoses the replacements; it
refuses a switch while an isolate of that function is active. An ordinary
client will not change shared scheduling resources on its own. Settings other
than the mode still follow the installation fingerprint rule: incompatible
changes require deliberate reinstallation. For a custom function name managed
directly through `FissionManager`, finish its isolates, call `uninstall()`, then
create it again with the new `Settings(anti_affinity=...)` value. The setup
command also accepts `--anti-affinity hard`; clients still need the matching
environment setting or `Settings` value.

Requests reserve scheduling capacity even when an isolate is idle. Finishing a
command does **not** release that reservation: the isolate keeps its filesystem
and Python environment. Call `kill()`, exit `manager.scope`, or use `manager.run()`
and `manager.chain()`, which destroy their isolate afterwards. Avoid sequentially
requesting more persistent isolates than the cluster can hold before releasing any
of them; such a caller can wait indefinitely by design.

Each active isolate holds a WebSocket, a client heartbeat thread and a worker pod.
Waiting callers do not own an initialized isolate. At large scale, account for
client threads/file descriptors, router connections, Kubernetes API traffic and
per-pod overhead as well as CPU/memory requests. A direct router URL can remove
WebSocket traffic from the Kubernetes API proxy. No claim of ten-thousand-isolate
capacity follows from a small-cluster test.

## Session loss and cleanup

* `kill()` interrupts outstanding socket I/O, stops the heartbeat and deletes the
  pod using its UID as a precondition. It waits for confirmed deletion and is
  safe to repeat. A cleanup timeout raises `CleanupError` instead of reporting
  success while the pod still exists.
* The worker removes its workspace, private venv, pip cache and process group when
  its connection closes or its session lease expires. Heartbeats keep idle
  isolates alive. Long commands do not have a fixed runtime deadline.
* A shared CronJob checks specialized pods once per minute by default. It removes
  closed or expired sessions; unreachable workers receive a persisted grace
  period (`janitor_unreachable_timeout`, default 120 seconds). A later healthy
  probe clears that timestamp. Job history and
  finished Job lifetimes are bounded. The janitor uses a separate service account
  with pod cleanup permissions.
* A lost connection invalidates the isolate. Commands are never replayed and a
  replacement pod never silently becomes the same isolate. Recreate explicitly
  from a previously saved snapshot if needed.

Kubernetes GET and DELETE requests retry transient connection failures and
429, 500, 502, 503 or 504 responses for up to `api_recovery_timeout`. This lets
pod checks and UID-guarded cleanup survive a short API-server restart. Other
writes and worker commands are never replayed. If the control plane stays
unavailable past that deadline, cleanup still reports failure; rerun
`manager.uninstall()` after it
recovers. A reachable direct `router_url` keeps the worker WebSocket outside the
API proxy, so an API-server restart need not close a healthy command session.
The janitor also retries a scan after a transient API timeout or 429/5xx response,
for up to `api_recovery_timeout` from the first failure. A repeated scan may
refresh an unreachable-since annotation, delaying deletion but never making it
premature; pod deletion uses a UID precondition. Worker health
probes retain their individual `janitor_request_timeout` without API retries.

A timed-out opening handshake can leave a specialized pod before the client learns
its identity. Allocation retries can succeed while that pod still exists. The
worker lease, janitor and Fission's fallback idle reaper reclaim these abandoned
pods asynchronously; `get()` does not wait for their deletion.

Janitor requests have individual timeouts. A scan can take longer than one minute
on a large cluster or when many pods are unreachable; overlapping scans are
forbidden. There is no total scan deadline that could repeatedly interrupt cleanup
before it reaches the same pods at the end of the list. The schedule is not a
guaranteed deletion deadline.

An unreachable Kubernetes node can delay physical process termination and pod
deletion until Kubernetes regains control of it. Neither the plugin nor its janitor
force-deletes pods to pretend that cleanup completed. Cleanup also depends on a
working control plane and enough capacity for the janitor Job to run.

An installation-scoped NetworkPolicy permits its janitor to reach its workers on
port 8888, alongside Fission's router and executor access. This is necessary when
Fission's ingress policies are enabled: a blocked health probe otherwise looks
like an unreachable worker. The policy covers only this installation's poolmgr
pods and is removed with the installation. Clusters with additional egress
restrictions must also allow janitor-to-worker and janitor-to-Kubernetes traffic.

The shared installation and its warm pool intentionally remain after the last
isolate is killed. They are reusable infrastructure, not per-run artifacts.
Remove them explicitly when no client needs the installation:

```python
manager.uninstall()
```

This terminates **all** isolates using that installation name. A ConfigMap owns
the shared resources; uninstall checks its ownership and waits for cascading
deletion. The worker bundle is stored in that ConfigMap, avoiding per-run package
uploads and Fission's 256 KiB inline-package limit. Bootstrap is idempotent across
clients. Different worker code or shared resource settings require a different
installation name or an explicit uninstall; active installations are not upgraded
in place. Client-only connection timeouts and polling intervals may differ.

## Settings

Settings are parsed with [skelet](https://github.com/mutating/skelet).
`FissionManager()` reads environment variables prefixed with `THRONGFISSION_`;
passing `settings=Settings(...)` uses the supplied settings instead. For example,
`THRONGFISSION_CPU_REQUEST=500m` changes `cpu_request`.

| Setting | Default | Purpose |
| --- | --- | --- |
| `namespace` | `fission-function` | Namespace watched by Fission for function resources |
| `python_name`, `debian_name`, `ubuntu_name` | `throngfission-python`, `throngfission-debian`, `throngfission-ubuntu` | Fixed shared function names; configurable through skelet |
| `name` | empty | Optional legacy explicit override for a manager; leave unset when using setup |
| `kubeconfig`, `context` | kubeconfig defaults | Kubernetes credentials and context |
| `router_namespace` | `fission` | Namespace containing the Fission router |
| `router_service` | `router:80` | Service used by the Kubernetes API proxy |
| `router_url` | empty | Optional direct HTTP(S)/WS(S) router URL |
| `image` | `python` | `python`, `debian`, `ubuntu`, or a full Fission runtime image reference |
| `cpu_request`, `memory_request` | `250m`, `256Mi` | Requests for each runtime container; Fission adds fetcher overhead |
| `anti_affinity` | `soft` | `none`, `soft`, or `hard` worker placement across nodes; use the same value for setup and clients |
| `pool_size` | `2` | Desired spare pool pods |
| `heartbeat_interval` | `5.0` seconds | Client heartbeat interval |
| `session_timeout` | `60.0` seconds | Worker session lease |
| `request_timeout` | `30.0` seconds | Individual API/socket operation timeout |
| `upload_timeout` | `45.0` seconds | Per-message timeout while uploading and initializing a workspace |
| `api_recovery_timeout` | `60.0` seconds | Retry window for transient Kubernetes GET/DELETE failures and janitor scan failures |
| `transfer_chunk_size` | `1048576` bytes (1 MiB) | Raw archive bytes per acknowledged upload/download message |
| `websocket_max_message_size` | `10485760` bytes (10 MiB) | Worker incoming WebSocket message limit; must fit a base64 chunk and its JSON envelope |
| `poll_interval` | `0.1` seconds | Command/capacity/deletion polling and janitor retry interval |
| `cleanup_timeout` | `120.0` seconds | Time allowed to confirm a deletion |
| `function_idle_timeout` | `120` seconds | Fission's fallback idle-pod reaping threshold |
| `stream_idle_timeout` | `60` seconds | Fission streaming idle setting; also determines router tap frequency |
| `stream_max_duration` | `0` | Maximum WebSocket duration in seconds; zero means unlimited |
| `socket_tracker_interval` | `1` second | Native Python runtime's closed-socket tracking interval |
| `termination_grace_period` | `1` second | Kubernetes termination grace for worker and janitor pods |
| `janitor_schedule` | `* * * * *` | Kubernetes CronJob schedule |
| `janitor_request_timeout` | `10.0` seconds | Timeout for each janitor API request and health probe |
| `janitor_unreachable_timeout` | `120.0` seconds | Grace after the first recorded failed health probe |
| `janitor_success_history`, `janitor_failure_history` | `0`, `1` | Retained successful/failed janitor Jobs |
| `janitor_job_ttl` | `60` seconds | Maximum lifetime after a janitor Job finishes, subject to controller delays |
| `janitor_backoff_limit` | `0` | Kubernetes retry count for a failed janitor Job |
| `janitor_cpu_request`, `janitor_memory_request` | `10m`, `32Mi` | Scheduling requests for each janitor container |

Setup additionally accepts the following settings, through the same
`THRONGFISSION_` prefix or matching CLI options such as `--setup-timeout 900`.
CLI values override environment values. All CLI values are read with skelet's
`FixedCLISource`; unknown arguments are rejected before any cluster operations.

| Setup setting | Default | Purpose |
| --- | --- | --- |
| `setup_timeout` | `600.0` seconds | Deadline for each control-plane/admission readiness check and certificate generation |
| `setup_poll_interval` | `1.0` second | Readiness polling interval |
| `diagnostic_timeout` | `300.0` seconds | Allocation and commands in each diagnostic |
| `image_timeout` | `600` seconds | Per-node image operation timeout; importer Job lifetime is three times this value |
| `image_cleanup_ttl` | `60` seconds | Finished administrative Job retention |
| `certificate_days` | `3650` | Lifetime of newly generated webhook certificates |
| `work_dir` | `.` | Existing directory for temporary build and certificate files |
| `apparmor_profile` | `auto` | Ubuntu 26.04 workaround detection; empty means runtime default; other values select a host profile |

These setup deadlines do not impose a deadline on ordinary isolate allocation.
Configure shared runtime settings identically for setup and clients. Changing
shared settings or worker code requires explicit removal of the affected preset
installation before setup can recreate it; existing isolates are never silently
reconfigured. `manager.uninstall()` removes only its selected runtime's shared
resources, not Fission, node images or the admission policy.

`session_timeout` must exceed `heartbeat_interval + request_timeout`.
`upload_timeout` applies to each acknowledged upload chunk and the final archive
extraction and Python environment initialization. If a single step can take longer
than the worker lease or router idle timeout, increase those settings as well.
The complete upload may take longer: each acknowledged chunk renews the session
lease. Snapshot downloads still use
`request_timeout`. Command duration is independent of these individual timeouts.

Workspace snapshots use uncompressed TAR, transferred as individually acknowledged
base64 chunks in both directions. The default chunks fit below Fission's GEVENT
server limit of 10 MiB per message, even for archives much larger than that limit.
`THRONGFISSION_TRANSFER_CHUNK_SIZE` and `THRONGFISSION_WEBSOCKET_MAX_MESSAGE_SIZE`
configure these sizes through skelet; smaller chunks can accommodate additional
router limits. Uploads validate the declared archive size before extraction and
check cancellation between chunks. An interrupted transfer invalidates the session
and is never replayed. Allocation failures close the connection and delete the pod.

The worker spools snapshots to temporary files, closing them after transfer or
session cleanup. Provision temporary disk space for the archive as well as the
extracted workspace. The public byte-artifact API still holds the full snapshot in
client memory; command output is also buffered. There is no total archive-size
limit imposed by the plugin, but available memory and disk space remain constraints.

`termination_grace_period` must be at least one second. The plugin applies it to
the Environment, pod and fetcher's `preStop` sleep together; otherwise Fission's
cluster-wide pod template can override the pod grace or leave a fetcher sleep
longer than the grace, which Kubernetes rejects.

All plugin timeouts, intervals, retention thresholds and resource requests are
settings. Worker and janitor configuration is also read through skelet. For
example, `THRONGFISSION_JANITOR_UNREACHABLE_TIMEOUT=300` allows five minutes after
the first failed probe; this grace is independent of `session_timeout`.
Changing shared resource settings requires a new installation name or uninstall.
Fission and Kubernetes controller intervals are cluster settings, rather than
plugin settings. Isolation invariants (`onceOnly`, one session per pod, no retained
specialized pods) and the janitor's non-overlapping scans are fixed.

Kubeconfig needs access to the selected namespace's Fission resources, ConfigMaps,
Secrets, ServiceAccounts, Roles, RoleBindings, NetworkPolicies, CronJobs and pods. The default
connection also needs service-proxy access in `router_namespace`. A direct router
receives the installation credential, but never kubeconfig credentials. Use TLS or
a trusted network. Isolates are ordinary containers for trusted commands, not a
security boundary for hostile code.

The runtime enables `MOCK_FETCHER` to bypass the Python image's legacy unsigned
socket-tracker callbacks. Fission 1.27's router manages streaming keepalives; the
plugin's heartbeat lease and janitor handle abandoned sessions independently.
The Function's `function_idle_timeout` must exceed
`min(stream_idle_timeout / 2, 30) + 5` seconds: Fission 1.27 caps router keepalives
at 30 seconds and batches taps every 5 seconds. Validation rejects unsafe
combinations; allow additional margin for a busy control plane. Normal `kill()` and
janitor cleanup delete pods directly, without waiting for that fallback threshold.

## Source layout

`throngfission/execution/` contains the Python API, the throng plugin, cluster
resource lifecycle, transport and state transfer. Its `worker/` subpackage runs
inside the cluster. The worker ZIP includes only that subpackage, the shared
archive and protocol modules, and their dependencies.

`throngfission/cli/` contains argument parsing, cluster setup, provisioning,
admission policies and installation assets. CLI code uses the execution API;
execution code does not import CLI modules. Each part owns its settings class.
Public imports remain `from throngfission import FissionManager, FissionIsolate, Settings`.

The worker archive participates in the installation fingerprint. Changing its
code or layout requires a new installation name or an explicit uninstall of the
previous installation before recreating it.

## Tests

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements_dev.txt -e .
.venv/bin/python -m pytest tests/units
.venv/bin/python -m pytest tests/typing
.venv/bin/python -m pytest tests/e2e -v
```

E2E tests require a Docker daemon. The session fixture in
`tests/e2e/conftest.py` creates a K3s server and two agents on one private
Docker network with testcontainers, runs the actual setup implementation, and
removes all three containers, their volumes and the network afterwards. The suite
covers first installation, a read-only repeated invocation, interrupted
diagnostics, missing Fission/function resources and different missing images on
different nodes. Destructive setup recovery cases run only in this disposable
cluster. On macOS with Colima:

```sh
colima start
DOCKER_HOST="unix://$HOME/.colima/default/docker.sock" \
TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE=/var/run/docker.sock \
.venv/bin/python -m pytest tests/e2e -v
```

To test an existing Fission cluster explicitly, use `--fission-existing`; each
test uses a unique installation name and removes its resources afterwards. The
resource-saturation and destructive setup recovery tests run only in the disposable cluster.

CI computes 100% line and branch coverage from **`tests/units` only**. E2E runs in a
separate Linux job without coverage collection. Type-only Protocol declarations
are excluded from executable coverage. Concurrency tests use locklib's SmartLock
and LockTraceWrapper to check deadlocks and lock boundaries.

Within `tests/units`, `tests/typing` and `tests/e2e`, test paths mirror the package:
for example, `execution/test_manager.py`, `cli/test_setup.py` and
`execution/worker/test_runtime.py`, wherever the suite has tests for that module.
Shared fixtures remain in the suite's `conftest.py`.

E2E covers discovery through pristan,
scope and chain cleanup, independent clients sharing an installation, cancellation,
transport/protocol failures, settings propagation, and scheduled cleanup after an
abrupt client-process exit. The repeated-allocation test performs ten cycles and
the long-command test runs for four minutes. These checks do not establish
multi-hour endurance or capacity for thousands of simultaneous isolates.
