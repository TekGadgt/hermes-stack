import { readFile, writeFile } from 'node:fs/promises';

const mcpPath = '/app/apps/daemon/dist/mcp.js';
const source = await readFile(mcpPath, 'utf8');

const toolAnchor = `    {
        name: 'get_project',`;
const handlerAnchor = `            case 'get_project': {`;

function replaceOnce(input, anchor, replacement, label) {
  const first = input.indexOf(anchor);
  const last = input.lastIndexOf(anchor);
  if (first < 0 || first !== last) {
    throw new Error(`Expected exactly one ${label} anchor`);
  }
  return input.slice(0, first) + replacement + input.slice(first);
}

const toolDefinition = `    {
        name: 'get_design_context',
        description: 'Pull the complete read-only Open Design context for the active or selected project: project metadata and custom instructions, selected skill guidance, selected design-system specification, canonical applied-plugin prompt context, and current artifact/file state. Call this before creating or substantially revising an OD artifact so the work benefits from OD design guidance without commissioning an OD agent run.',
        inputSchema: {
            type: 'object',
            properties: {
                project: PROJECT_ARG,
                includeFiles: {
                    type: 'boolean',
                    description: 'Include current project file metadata and artifact manifests. Defaults to true.',
                },
                maxGuidanceBytes: {
                    type: 'number',
                    description: 'Soft cap for combined skill, design-system, and plugin guidance text. Defaults to 200000; maximum 500000.',
                },
            },
            additionalProperties: false,
        },
        annotations: { ...READ_ANNOTATIONS, title: 'Pull Open Design context' },
    },
`;

const handler = `            case 'get_design_context': {
                const { id, resolved, active } = await resolveProjectArg(baseUrl, args.project, headers);
                const projectData = await getJson(\`${'${baseUrl}'}/api/projects/${'${encodeURIComponent(id)}'}\`, headers);
                const project = projectData?.project ?? projectData;
                const skillId = project?.skillId ?? project?.metadata?.skillId ?? null;
                const designSystemId = project?.designSystemId ?? project?.metadata?.designSystemId ?? null;
                const snapshotId = project?.appliedPluginSnapshotId ?? null;
                const optionalJson = async (url) => {
                    try {
                        return { data: await getJson(url, headers), error: null };
                    }
                    catch (error) {
                        return { data: null, error: String(error?.message ?? error) };
                    }
                };
                const [skillResult, designSystemResult, pluginResult, filesResult] = await Promise.all([
                    skillId
                        ? optionalJson(\`${'${baseUrl}'}/api/skills/${'${encodeURIComponent(skillId)}'}\`)
                        : Promise.resolve({ data: null, error: null }),
                    designSystemId
                        ? optionalJson(\`${'${baseUrl}'}/api/design-systems/${'${encodeURIComponent(designSystemId)}'}\`)
                        : Promise.resolve({ data: null, error: null }),
                    snapshotId
                        ? optionalJson(\`${'${baseUrl}'}/api/applied-plugins/${'${encodeURIComponent(snapshotId)}'}/canon\`)
                        : Promise.resolve({ data: null, error: null }),
                    args.includeFiles === false
                        ? Promise.resolve({ data: null, error: null })
                        : optionalJson(\`${'${baseUrl}'}/api/projects/${'${encodeURIComponent(id)}'}/files\`),
                ]);
                const skill = skillResult.data?.skill ?? skillResult.data;
                const designSystem = designSystemResult.data?.designSystem ?? designSystemResult.data;
                const plugin = pluginResult.data;
                const requestedCap = typeof args.maxGuidanceBytes === 'number' && Number.isFinite(args.maxGuidanceBytes)
                    ? Math.floor(args.maxGuidanceBytes)
                    : 200000;
                let remainingBytes = Math.max(1000, Math.min(500000, requestedCap));
                const truncations = [];
                const takeGuidance = (kind, value) => {
                    if (typeof value !== 'string' || value.length === 0)
                        return null;
                    const bytes = Buffer.byteLength(value, 'utf8');
                    if (bytes <= remainingBytes) {
                        remainingBytes -= bytes;
                        return value;
                    }
                    const clipped = Buffer.from(value, 'utf8').subarray(0, Math.max(0, remainingBytes)).toString('utf8');
                    truncations.push({ kind, originalBytes: bytes, returnedBytes: Buffer.byteLength(clipped, 'utf8') });
                    remainingBytes = 0;
                    return clipped;
                };
                const skillBody = takeGuidance('skill', skill?.body ?? skill?.content ?? null);
                const designSystemBody = takeGuidance('designSystem', designSystem?.body ?? designSystem?.content ?? null);
                const pluginBlock = takeGuidance('plugin', plugin?.block ?? null);
                const warnings = [
                    ...(skillResult.error ? [{ kind: 'skill', id: skillId, error: skillResult.error }] : []),
                    ...(designSystemResult.error ? [{ kind: 'designSystem', id: designSystemId, error: designSystemResult.error }] : []),
                    ...(pluginResult.error ? [{ kind: 'plugin', id: snapshotId, error: pluginResult.error }] : []),
                    ...(filesResult.error ? [{ kind: 'files', error: filesResult.error }] : []),
                ];
                return ok(withActiveEcho({
                    contextVersion: 1,
                    project: {
                        id: project?.id ?? id,
                        name: project?.name ?? null,
                        skillId,
                        designSystemId,
                        appliedPluginSnapshotId: snapshotId,
                        customInstructions: project?.customInstructions ?? null,
                        pendingPrompt: project?.pendingPrompt ?? null,
                        metadata: project?.metadata ?? null,
                        createdAt: project?.createdAt ?? null,
                        updatedAt: project?.updatedAt ?? null,
                        resolvedDir: projectData?.resolvedDir ?? null,
                    },
                    guidance: {
                        precedence: ['project.customInstructions', 'appliedPlugin.canonicalPrompt', 'selectedSkill.body', 'selectedDesignSystem.body'],
                        selectedSkill: skillId
                            ? { id: skillId, name: skill?.name ?? skill?.title ?? skillId, description: skill?.description ?? null, body: skillBody }
                            : null,
                        selectedDesignSystem: designSystemId
                            ? { id: designSystemId, name: designSystem?.title ?? designSystem?.name ?? designSystemId, summary: designSystem?.summary ?? null, body: designSystemBody, packageInfo: designSystem?.packageInfo ?? null }
                            : null,
                        appliedPlugin: snapshotId
                            ? { snapshotId, pluginId: plugin?.pluginId ?? null, canonicalPrompt: pluginBlock }
                            : null,
                    },
                    artifactState: filesResult.data,
                    truncations,
                    warnings,
                    note: 'This is OD design guidance and project state for the calling agent. It does not execute an OD run or invoke Vela.',
                }, active, resolved));
            }
`;

let patched = replaceOnce(source, toolAnchor, toolDefinition, 'tool definition');
patched = replaceOnce(patched, handlerAnchor, handler, 'tool handler');
await writeFile(mcpPath, patched);
console.log('Added read-only get_design_context to the Open Design MCP bridge');
