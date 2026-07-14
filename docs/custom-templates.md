# Custom job templates

A template turns a workflow into a one-click job: a Docker image, a command
with `{{parameter}}` placeholders, and a parameter schema that becomes a
form on the Jobs page. Manifold ships nine; this page is about writing your
own — by hand, or by having an agent write one for you.

The point is self-sufficiency. The first time you do something new (say,
turning 2D concept art into 3D renders), do it WITH an agent: give it the
goal, let it work on the instance, iterate until the pipeline is right.
Then have it save the working pipeline as a template. From that day on it
is a form on the Jobs page — no agent, no tokens, no re-explaining. The
agent is scaffolding, not a dependency.

## Where they live

- The dashboard: **Jobs → Custom templates → New template.** A YAML editor
  with validation on save; errors come back verbatim.
- An agent: the MCP tool `save_template(yaml_text)`. A good prompt:
  "We proved this pipeline works. Save it as a Manifold template named
  `sketch-to-3d`, parameterizing the input directory and the resolution."
- On disk: one YAML file per template in `custom-templates/` under the data
  dir (next to `manifold.db`). They are files, not database rows — back
  them up, commit them to your own repo, share them.

Saved templates are live immediately (no restart), appear in the Jobs
template picker, and work with every existing mechanism: parameter
validation at enqueue, auto-manage, the cost estimate, Autopilot's
`run_job`, image preflight. A custom template with the same name as a
bundled one overrides it; deleting the custom copy restores the original.

## The format

```yaml
name: sketch-to-3d
description: Turn 2D concept images into 3D mesh renders.
image: pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime
# {{placeholders}} are filled from the parameter form and shell-quoted.
# The bash -c '...' argv0 pattern keeps $vars for the CONTAINER shell.
command: >-
  bash -c 'python /work/render.py --in /images --out /out
  --resolution {{resolution}}' argv0
parameters:
  - name: input_dir
    type: string            # string | integer | number | boolean
    description: Directory of source images under the filesystem
    # no default = required
  - name: resolution
    type: integer
    description: Output resolution
    default: 512
volumes:
  # {persistent} expands to /lambda/nfs/<your filesystem> at dispatch.
  - host: "{persistent}/{{input_dir}}"
    container: /images
    read_only: true
  - host: "{persistent}/renders"
    container: /out
env:
  HF_HOME: /workspace/ephemeral/hf-cache
gpu:
  min_vram_gib: 24
```

## The rules (same jail as the bundled set)

Custom templates get no powers a bundled template lacks. The loader
enforces, at save time:

- **Mounts** only under `{persistent}` or `/workspace/ephemeral`. A
  template asking for `/etc` or `/home` is rejected, whoever wrote it.
- **Ports**, if declared, are always published on the instance's loopback.
  Nothing a template says can open a public listener.
- **Placeholders** must all be declared parameters, and every substituted
  value is shell-quoted at dispatch.

Budget, concurrency, and the termination data rescue apply to jobs from
custom templates exactly as to everything else.

## Tips

- Put model caches in `/workspace/ephemeral` (fast, free, dies with the
  box) and results under `{persistent}` (survives).
- Prefer parameters over hardcoded values — a parameterized template
  serves every future variation of the task.
- Test with the cheapest GPU that fits, then let the History page's
  right-size hint tell you if you over-provisioned.
