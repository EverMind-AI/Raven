import { client } from './client';
import type {
	CreateCredentialRequest,
	CreateCredentialResponse,
	CredentialListResponse,
	CredentialView,
	CredentialSchemasResponse,
	ListModelResponse,
	ModelCardConfig,
	UpdateCredentialRequest,
} from './types';

export const credentialApi = {
	list: () => client.get<CredentialListResponse>('/credential/'),

	schemas: () => client.get<CredentialSchemasResponse>('/credential/schemas'),

	create: (body: CreateCredentialRequest) =>
		client.post<CreateCredentialResponse>('/credential/', body),

	update: (credentialId: string, body: UpdateCredentialRequest) =>
		client.patch<CredentialView>(`/credential/${credentialId}`, body),

	delete: (credentialId: string) => client.delete(`/credential/${credentialId}`),

	listModels: (credentialId: string) =>
		client.get<ListModelResponse>(`/credential/${credentialId}/models`),

	updateModels: (credentialId: string, models: ModelCardConfig[] | null) =>
		client.put<ListModelResponse>(`/credential/${credentialId}/models`, { models }),
};
