# Daze HomeAssistant Addon

Daze is a wallbox controllable via Mobile app and webapp at [webportal.dazeservice.com](https://webportal.dazeservice.com).

This app (previously addon) integrates Daze wallbox controls and metrics into Home Assistant.

The initial access_token and refresh token will be provided by the user manually during installation. But as the auth token is short lived, we'll keep updating this value thanks to refresh token

The Goal is to watch the Wallbox status (current, charging/not charging)
and control it ( change current, pause/play )


# Auth method:

Bearer with provided access_token

# Breakdown of API calls

1) Get User INFO
Request:
GET https://daze.auth.eu-central-1.amazoncognito.com/oauth2/userInfo
Response:
{
    "custom:usertype": "",
    "custom:country": "",
    "sub": "",
    "email_verified": "",
    "address": "",
    "custom:cap": "3530",
    "custom:city": " ",
    "custom:phone": "",
    "identities": "[{\"dateCreated\":\"1762526839478\",\"userId\":\"113879948999503992216\",\"providerName\":\"Google\",\"providerType\":\"Google\",\"issuer\":null,\"primary\":\"false\"}]",
    "name": "",
    "family_name": "",
    "email": "",
    "username": ""
}

2) Get Network list

curl 'https://webapi.dazeservice.com/v3/users/andrea.restello@gmail.com/networks?includeStats=true' \
  -H 'accept: application/json, text/plain, */*' \
  -H 'authorization: Bearer' 

{"data":[{"numEvsesInNetwork":1,"numUsersInNetwork":1,"numRfidsInNetwork":0,"isAdmin":true,"id":null,"uid":"ad85377e-f30c-4554-bd5c-47be640d740b","name":"casa","description":null,"address":"via martiri libertà 34","city":"rubano","zipCode":"35030","country":"Italy","networkType":3,"gridIsThreePhase":false,"supplyMaxPower":4500,"chargersMaxPower":0,"isPhotovoltaic":true,"isPhotovoltaicThreePhase":false,"isAccumulation":false,"accumulationMaxPower":0,"arera":false,"updated":"2026-05-15T09:07:46.415295Z","isDeleted":false,"slaveNumber":0,"energyCostMulByThousand":350,"priceActivationMulByThousand":null,"priceEnergyMulByThousand":null,"priceMinuteChargingMulByThousand":null,"priceMinutePostChargingMulByThousand":null,"smartTariffEnabled":false,"ecoModeType":0,"networkRechargeModality":0,"ecoModeEnabled":false,"ecoSchedule":[],"timeZone":"Europe/Berlin","selfConsumptionEnabledOutOfTimeSlot":false,"threePhaseAutoSwitchEnabled":false,"emsConfiguration":0,"ems":null,"currency":{"code":"EUR","symbol":"€"}}],"message":"","errors":[]}


3) Get the Chargers


curl 'https://webapi.dazeservice.com/v3/networks/ad85377e-f30c-4554-bd5c-47be640d740b/evses?includeEcoInfo=false' \
  -H 'accept: application/json, text/plain, */*' \

{
    "data": [
        {
            "id": null,
            "serialNumber": "24DT0102958",
            "deviceProfile": "DT01",
            "deviceProfileId": "62a1cd50-fe3b-11ed-811c-b9fea7ea4299",
            "evseName": "casa",
            "arera": false,
            "dpm": true,
            "autostart": true,
            "photovoltaic": true,
            "supplyGrid3F": false,
            "supplyGridMaxPower": 4500,
            "softwareVersion": "22.4.0",
            "firmwareVersion": "13.3.0",
            "assignedFirmwarePackage": {
                "id": "f4dd0da0-9926-11f0-8ed7-49bbef6b42eb",
                "type": 0,
                "sizeInBytes": 120544,
                "tag": "DT01_STM 13.3.0",
                "title": "DT01_STM",
                "version": "13.3.0",
                "checksum": "304490a09920313b017a041b048715fec080617b86d020c643f0acd2cc407dcf",
                "checksumAlgorithm": "SHA256",
                "deviceProfileId": "62a1cd50-fe3b-11ed-811c-b9fea7ea4299"
            },
            "assignedSoftwarePackage": {
                "id": "da1f0b80-9926-11f0-8ed7-49bbef6b42eb",
                "type": 1,
                "sizeInBytes": 2184336,
                "tag": "DT01_ESP 22.4.0",
                "title": "DT01_ESP",
                "version": "22.4.0",
                "checksum": "f4a7988c92e8228e0f4df68ad2fc490e12096c22cbc763749341abdd18276ade",
                "checksumAlgorithm": "SHA256",
                "deviceProfileId": "62a1cd50-fe3b-11ed-811c-b9fea7ea4299"
            },
            "sccLimit": 0,
            "evseIsThreePhase": false,
            "updated": "2026-05-16T09:44:11.001442Z",
            "schedules": [
                {
                    "day": 0,
                    "start1": "12:00:00",
                    "end1": "16:30:00",
                    "start2": null,
                    "end2": null,
                    "start3": null,
                    "end3": null
                },
                {
                    "day": 6,
                    "start1": "12:00:00",
                    "end1": "16:30:00",
                    "start2": null,
                    "end2": null,
                    "start3": null,
                    "end3": null
                },
                {
                    "day": 2,
                    "start1": "11:00:00",
                    "end1": "16:00:00",
                    "start2": null,
                    "end2": null,
                    "start3": null,
                    "end3": null
                },
                {
                    "day": 1,
                    "start1": "11:00:00",
                    "end1": "16:00:00",
                    "start2": null,
                    "end2": null,
                    "start3": null,
                    "end3": null
                }
            ],
            "wifiEnabled": true,
            "wifiSSID": "TP-LINK_RESTELLO",
            "scheduling": false,
            "isOcppMode": false,
            "ocppServer": "",
            "hasOcppPassword": false,
            "canBeAutomaticallyUpdate": true,
            "lastStatus": 3,
            "operationMode": 3,
            "isMode2On": false,
            "active": true,
            "acPhaseLineId1": 1,
            "acPhaseLineId2": 0,
            "acPhaseLineId3": 0,
            "gridMaxCurrentL1": 0,
            "gridMaxCurrentL2": 0,
            "gridMaxCurrentL3": 0,
            "warrantyExpiration": "2027-01-16T00:00:00Z",
            "lastTime": "01:02:37",
            "lastSupplyGridInstantCurrentL1": 2637,
            "lastSupplyGridInstantCurrentL2": 190,
            "lastSupplyGridInstantCurrentL3": 223,
            "lastMaxInstallationCurrent": 32000,
            "ecoModeEnabled": false,
            "threePhaseAutoSwitchOn": false,
            "dryContact": 0,
            "supplyGridCurrentExtendedRangeSensorOn": false,
            "supplyGridSensorType": 0,
            "isDynamicLoadManagementOn": false,
            "powerSharingMasterManagement": 0,
            "localMasterStaticAvailablePower": null,
            "maxThreePhaseImbalanceInMilliAmps": 0,
            "maxExternalChargingCurrentInMilliAmps": 9130,
            "apn": null,
            "createdByEmail": null,
            "brightness": null,
            "connectivityTypeForScenario": 0,
            "evseTypology": 0,
            "lcdLanguage": "Italian",
            "sockets": [
                {
                    "id": "b23f7870-7cdf-11ef-93a4-2725cc2d4572",
                    "serialNumber": "24DT0102958",
                    "deviceToken": null,
                    "evseIsThreePhase": false,
                    "lastEnergy": 2208,
                    "ocppId": "",
                    "lastTime": "01:02:37",
                    "lastPower": 2077,
                    "lastUId": "",
                    "lastSessionIdAsDateTime": "2026-05-16T09:53:16Z",
                    "lastSessionId": 1778925196000,
                    "slaveId": 0,
                    "lastStatus": 3,
                    "operationMode": 3,
                    "lastChargingCurrentInstantL1": 9052,
                    "lastChargingCurrentInstantL2": 0,
                    "lastChargingCurrentInstantL3": 0,
                    "lastEVSESuspensionReason": 0,
                    "lastEVSESystemError": 0,
                    "ecoChargeManuallyPaused": 0,
                    "maxExternalChargingCurrentInMilliAmps": 9130,
                    "isPrimary": true,
                    "lastMaxChargingCurrent": 9130,
                    "smartTariffSessionStartUtc0": "1970-01-01T00:00:00Z",
                    "lastACVoltageL1": 229,
                    "lastACVoltageL2": 7,
                    "lastACVoltageL3": 6,
                    "lastBoardL1Temperature": 32,
                    "lastCaseTemperature": 34,
                    "lastFanStatus": null,
                    "lastMultipleEVSEMaxInstallationCurrent": null,
                    "lastOCPPState": null,
                    "lastSlaveConnectionStatusToMaster": null,
                    "lastUpdateStatus": null,
                    "active": true,
                    "lastAttributesUpdatedOn": "2026-05-16T10:56:56.424646Z"
                }
            ]
        }
    ],
    "message": "",
    "errors": []
}

4) Get Recharge Sessions


curl 'https://webapi.dazeservice.com/v3/networks/ad85377e-f30c-4554-bd5c-47be640d740b/rechargeSessions?TotalLimit=1000&LimitPerPage=4' \
  -H 'accept: application/json, text/plain, */*' 

{
    "data": [
        {
            "id": "b959d3ff-1bd9-4568-9f7d-6f3eda389ce3",
            "evseName": "casa",
            "serialNumber": "24DT0102958",
            "socketSerialNumber": "24DT0102958",
            "sessionId": 1778924375000,
            "sessionEnd": 1778925178000,
            "totEnergy": 462,
            "averagePow": 2226,
            "chargeTime": "00:12:27",
            "evseId": "b23f7870-7cdf-11ef-93a4-2725cc2d4572",
            "user": "",
            "email": "",
            "uid": "",
            "authenticationStatus": 0,
            "isAdmin": true,
            "sessionType": 6,
            "networkName": "casa",
            "rfidSerialNumber": "",
            "startDate": "2026-05-16T09:39:35Z",
            "endDate": "2026-05-16T09:52:58Z",
            "telemetryDate": "2026-05-16T09:39:40.517052Z",
            "computedEnergyCostMulByThousand": 161700,
            "currency": {
                "code": "EUR",
                "symbol": "€"
            },
            "smartTariffSession": null,
            "isAveragePowValid": true,
            "priceMulByThousand": 0,
            "timezone": "Europe/Berlin"
        },
}


5) Charger Infos

curl 'https://webapi.dazeservice.com/v3/sockets/24DT0102958/remoteInfo?includeEcoInfo=true&includeNextSchedule=true'


{
    "data": {
        "active": true,
        "evseState": 3,
        "evseSuspensionReason": 0,
        "evseSystemError": 0,
        "chargeSession": {
            "deliveredEnergyAsWattHour": 2208,
            "instantPowerAsWatt": 2077,
            "startTime": "2026-05-16T09:53:16Z",
            "chargeTime": "01:02:37",
            "user": null,
            "sessionId": 1778925196000,
            "lastChargingCurrentInstantL1": 9052,
            "lastChargingCurrentInstantL2": 0,
            "lastChargingCurrentInstantL3": 0,
            "lastMaxChargingCurrent": 9130,
            "lastACVoltageL1": 229,
            "lastACVoltageL2": 7,
            "lastACVoltageL3": 6,
            "currentlyChargingInThreePhase": false
        },
        "smartTariffBatteryInfo": null,
        "nextScheduleInfo": null,
        "evseIsThreePhase": false,
        "isPaused": false,
        "isScheduledPaused": false,
        "isSmartTariffPaused": false
    },
    "message": "",
    "errors": []
}

6) Set Max Charging Current

curl 'https://webapi.dazeservice.com/v3/evses/24DT0102958/configurations/maxExternalChargingCurrent' \
  -H 'accept: application/json, text/plain, */*' \
  -H 'authorization: Bearer ' \
  -H 'content-type: application/json' \
  --data-raw '{"evseSerialNumber":"24DT0102958","maxExternalChargingCurrentInMilliAmps":9565}'

{"message":"","errors":[]}


7) Pause charge

curl 'https://webapi.dazeservice.com/v3/sockets/24DT0102958/commands/stopcharge' \
  --data-raw '{}'

{"message":"","errors":[]}


8) Start charge

curl 'https://webapi.dazeservice.com/v3/sockets/24DT0102958/commands/playcharge' \
  -H 'accept: application/json, text/plain, */*' \
  -H 'authorization: Bearer ' \
  --data-raw '{}'

9) Refresh TOKEN

curl 'https://daze.auth.eu-central-1.amazoncognito.com/oauth2/token' \
  -H 'accept: */*' \
  -H 'accept-language: en-US,en-GB;q=0.9,en;q=0.8,it;q=0.7' \
  -H 'content-type: application/x-www-form-urlencoded;charset=UTF-8' \
  --data-raw 'client_id=4m0rp7oqarbrc3hn67ivvonba8&redirect_uri=https%3A%2F%2Fwebportal.dazeservice.com%2Fauthentication%2Fcallback&grant_type=refresh_token&refresh_token=XXXX'