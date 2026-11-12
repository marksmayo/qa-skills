// Deliberately misconfigured: every block below has a planted problem for the built-in patterns.
param location string = resourceGroup().location
param dbAdminPassword string
param opsPrincipalId string

resource db 'Microsoft.DBforPostgreSQL/flexibleServers@2023-06-01-preview' = {
  name: 'ferry-db'
  location: location
  properties: {
    administratorLogin: 'ferryadmin'
    administratorLoginPassword: dbAdminPassword
    network: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

resource allowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-06-01-preview' = {
  parent: db
  name: 'AllowAllAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'ferry-kv'
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    accessPolicies: [
      {
        tenantId: subscription().tenantId
        objectId: opsPrincipalId
        permissions: {
          secrets: [
            'get'
            'list'
          ]
        }
      }
    ]
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: 'ferryfiles'
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_0'
  }
}

resource site 'Microsoft.Web/sites@2023-01-01' = {
  name: 'ferry-ops-api'
  location: location
  properties: {
    siteConfig: {
      ftpsState: 'AllAllowed'
      minTlsVersion: '1.0'
      appSettings: [
        {
          name: 'STORAGE_CONNECTION'
          value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value}'
        }
      ]
    }
  }
}
