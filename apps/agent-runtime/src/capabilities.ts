/** Adapter for Pi's public search extension. No separate ranker or executor.
 * Search projections do not change registered tools or their execution schemas.
 * Not enabled by runtime.ts until Host discovery receipts/protocol are wired.
 */
import {createToolSearchExtension} from '@earendil-works/pi-coding-agent';
import type {ExtensionAPI, ExtensionFactory, ToolInfo} from '@earendil-works/pi-coding-agent';
import {canonical} from './tool-bridge.js';
import type {Json} from './protocol.js';

const SEARCH = 'tool_search';
const segmenter = new Intl.Segmenter('zh', {granularity:'word'});

/** Deterministic search-only Unicode tokens, not translation or business aliases. */
export function searchProjection(text:string): string {
  const words = [...segmenter.segment(text.normalize('NFKC'))]
    .filter(part => part.isWordLike && /[^\x00-\x7f]/u.test(part.segment))
    .map(part => 'uword' + [...part.segment].map(char => char.codePointAt(0)!.toString(16)).join('x'));
  return text + '\n' + words.join(' ');
}

function schemaProjection(value:unknown): unknown {
  if (Array.isArray(value)) return value.map(schemaProjection);
  if (value !== null && typeof value === 'object') return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [key,
      key === 'description' && typeof item === 'string' ? searchProjection(item) : schemaProjection(item)]));
  return value;
}

function fingerprint(tool:ToolInfo): string {
  return canonical({name:tool.name, description:tool.description,
    parameters:tool.parameters, exposure:tool.exposure,
    namespace:tool.namespace ?? null} as unknown as Json);
}

export interface DiscoveryBoundary {
  /** Must check the same Host catalog pin/current authority, not a model claim. */
  verify(): Promise<void>;
  /** Runtime integration must stop subsequent model/tool dispatch on failure. */
  fail(error:Error): void;
  selected?(callId:string, arguments_:Record<string,unknown>, names:string[], signal?:AbortSignal):Promise<void>;
}

export function createAuthorizedDiscovery(tools:readonly ToolInfo[], boundary:DiscoveryBoundary, searchTool?:{description:string;parameters:ToolInfo["parameters"]}): ExtensionFactory {
  const frozen = structuredClone(tools);
  const pins = new Map(frozen.map(tool => [tool.name, fingerprint(tool)]));
  if (pins.size !== frozen.length || frozen.some(tool => tool.name === SEARCH || tool.exposure !== 'deferred'))
    throw new Error('discovery_catalog_invalid');
  const deny = ():never => { throw new Error('discovery_catalog_changed'); };
  return pi => {
    const checkActive = (names:readonly string[]) => {
      if (!names.includes(SEARCH) || new Set(names).size !== names.length
          || names.some(name => name !== SEARCH && !pins.has(name))) deny();
    };
    const current = ():ToolInfo[] => {
      checkActive(pi.getActiveTools());
      const actual = pi.getAllTools().filter(tool => tool.name !== SEARCH);
      if (actual.length !== pins.size || new Set(actual.map(tool=>tool.name)).size !== pins.size
          || actual.some(tool => pins.get(tool.name) !== fingerprint(tool))) deny();
      return actual;
    };
    const facade:ExtensionAPI = {...pi,
      getActiveTools:() => { current(); return [...pi.getActiveTools()]; },
      getAllTools:() => current().map(tool => ({...tool,
        description:searchProjection(tool.description),
        parameters:schemaProjection(tool.parameters) as ToolInfo['parameters'],
        ...(tool.namespace ? {namespace:{...tool.namespace,
          description:searchProjection(tool.namespace.description ?? ''),
          instructions:searchProjection(tool.namespace.instructions ?? '')}} : {}),
      })),
      setActiveTools:names => { current(); checkActive(names); pi.setActiveTools([...names]); },
      registerTool:definition => {
        if (definition.name !== SEARCH || definition.defaultActive !== false) deny();
        pi.registerTool({...definition, ...(searchTool ? {description:searchTool.description, parameters:searchTool.parameters as typeof definition.parameters} : {}), execute:async (...args) => {
          try {
            await boundary.verify(); current();
            const [id, input, ...rest] = args;
            if (typeof input.query !== 'string' || !input.query.trim()) throw new Error('discovery_query_invalid');
            const result = await definition.execute(id, {...input, query:searchProjection(input.query)}, ...rest);
            const details=result.details as {loaded?:unknown};
            if (!Array.isArray(details.loaded) || details.loaded.some(name=>typeof name !== 'string')) deny();
            await boundary.selected?.(id, structuredClone(input), details.loaded as string[], args[2]);
            await boundary.verify(); current();
            return result;
          } catch (error) {
            const failure = error instanceof Error ? error : new Error('discovery_failed');
            boundary.fail(failure); throw failure;
          }
        }});
      },
    };
    return createToolSearchExtension()(facade);
  };
}
