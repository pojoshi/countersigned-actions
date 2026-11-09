import policyJson from '../../src/countersigned/default_policy.json';
import { policyVersion, type Json } from './canonical';
import type { Policy } from './engine';

// One policy file for both implementations: the Python package ships it, the playground imports it.
export const DEFAULT_POLICY = policyJson as unknown as Policy;
export const DEFAULT_POLICY_VERSION = policyVersion(policyJson as unknown as Json);
