[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TemplatePath,
    [string]$ParameterName = "OPENAI_API_KEY"
)

$ErrorActionPreference = "Stop"
$resolved = (Resolve-Path -LiteralPath $TemplatePath).Path
$template = Get-Content -LiteralPath $resolved -Raw | ConvertFrom-Json
$parameter = $template.parameters.$ParameterName

if ($null -eq $parameter) {
    Write-Output "$ParameterName=ABSENT"
    exit 0
}

$candidateValues = @()
if ($null -ne $parameter.defaultValue.value) {
    $candidateValues += [string]$parameter.defaultValue.value
}
if ($null -ne $parameter.conditionalValues) {
    foreach ($property in $parameter.conditionalValues.PSObject.Properties) {
        if ($null -ne $property.Value.value) {
            $candidateValues += [string]$property.Value.value
        }
    }
}

$hasNonEmptyValue = $candidateValues.Where({ -not [string]::IsNullOrWhiteSpace($_) }).Count -gt 0
if ($hasNonEmptyValue) {
    Write-Output "$ParameterName=PRESENT_WITH_NONEMPTY_VALUE"
    exit 2
}

Write-Output "$ParameterName=PRESENT_WITHOUT_NONEMPTY_VALUE"
exit 1
