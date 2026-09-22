-- A stand-in NightArc database so you can test the SQL ingester on the Debian box
-- without touching a client's real instance. The shape mirrors what a bat-acoustics
-- schema looks like: sites, deployments, recordings, auto/manual classified calls.
--
--   docker exec -i mssql /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa \
--     -P "$SA_PASSWORD" -C -i /dev/stdin < scripts/lab-nightarc-fixture.sql
--
-- Adjust to your real schema once you have read it. NEVER run this against production.

IF DB_ID('NightArc') IS NULL CREATE DATABASE NightArc;
GO
USE NightArc;
GO

IF OBJECT_ID('dbo.Calls') IS NOT NULL DROP TABLE dbo.Calls;
IF OBJECT_ID('dbo.Recordings') IS NOT NULL DROP TABLE dbo.Recordings;
IF OBJECT_ID('dbo.Deployments') IS NOT NULL DROP TABLE dbo.Deployments;
IF OBJECT_ID('dbo.Detectors') IS NOT NULL DROP TABLE dbo.Detectors;
IF OBJECT_ID('dbo.Sites') IS NOT NULL DROP TABLE dbo.Sites;
IF OBJECT_ID('dbo.Species') IS NOT NULL DROP TABLE dbo.Species;
GO

CREATE TABLE dbo.Sites (
    SiteId        INT IDENTITY(1,1) PRIMARY KEY,
    SiteCode      NVARCHAR(20)  NOT NULL UNIQUE,
    SiteName      NVARCHAR(120) NOT NULL,
    -- British National Grid, as most UK ecology work is done in EPSG:27700
    Easting       INT           NULL,
    Northing      INT           NULL,
    HabitatNotes  NVARCHAR(MAX) NULL,
    ClientRef     NVARCHAR(40)  NULL
);

CREATE TABLE dbo.Species (
    SpeciesId     INT IDENTITY(1,1) PRIMARY KEY,
    Code          NVARCHAR(12)  NOT NULL UNIQUE,   -- e.g. PIPPIP
    ScientificName NVARCHAR(90) NOT NULL,
    CommonName    NVARCHAR(90)  NOT NULL,
    IsAnnexII     BIT           NOT NULL DEFAULT 0
);

CREATE TABLE dbo.Detectors (
    DetectorId    INT IDENTITY(1,1) PRIMARY KEY,
    SerialNumber  NVARCHAR(40)  NOT NULL,
    Model         NVARCHAR(60)  NOT NULL,          -- SM4BAT-FS, Anabat Swift...
    FirmwareVer   NVARCHAR(20)  NULL
);

CREATE TABLE dbo.Deployments (
    DeploymentId  INT IDENTITY(1,1) PRIMARY KEY,
    SiteId        INT NOT NULL REFERENCES dbo.Sites(SiteId),
    DetectorId    INT NOT NULL REFERENCES dbo.Detectors(DetectorId),
    StartUtc      DATETIME2 NOT NULL,
    EndUtc        DATETIME2 NULL,
    SurveyorName  NVARCHAR(90) NULL,
    WeatherNotes  NVARCHAR(MAX) NULL
);

CREATE TABLE dbo.Recordings (
    RecordingId   BIGINT IDENTITY(1,1) PRIMARY KEY,
    DeploymentId  INT NOT NULL REFERENCES dbo.Deployments(DeploymentId),
    FileName      NVARCHAR(260) NOT NULL,
    RecordedUtc   DATETIME2 NOT NULL,
    DurationMs    INT NULL,
    SampleRateHz  INT NULL,
    -- Kaleidoscope Pro 5.9+ writes MetaForm XML into the guano/metadata block
    MetaFormXml   NVARCHAR(MAX) NULL
);
CREATE INDEX IX_Recordings_RecordedUtc ON dbo.Recordings(RecordedUtc);

CREATE TABLE dbo.Calls (
    CallId        BIGINT IDENTITY(1,1) PRIMARY KEY,
    RecordingId   BIGINT NOT NULL REFERENCES dbo.Recordings(RecordingId),
    SequenceNo    INT NOT NULL,
    StartMs       INT NOT NULL,
    PeakFreqKhz   DECIMAL(6,2) NULL,
    AutoSpeciesId INT NULL REFERENCES dbo.Species(SpeciesId),
    AutoConfidence DECIMAL(5,4) NULL,
    ManualSpeciesId INT NULL REFERENCES dbo.Species(SpeciesId),
    ReviewerNotes NVARCHAR(MAX) NULL
);
CREATE INDEX IX_Calls_Recording ON dbo.Calls(RecordingId);
GO

INSERT INTO dbo.Species (Code, ScientificName, CommonName, IsAnnexII) VALUES
 ('PIPPIP','Pipistrellus pipistrellus','Common pipistrelle',0),
 ('PIPPYG','Pipistrellus pygmaeus','Soprano pipistrelle',0),
 ('NYCNOC','Nyctalus noctula','Noctule',0),
 ('RHIHIP','Rhinolophus hipposideros','Lesser horseshoe bat',1),
 ('BARBAR','Barbastella barbastellus','Barbastelle',1),
 ('MYOSPP','Myotis sp.','Myotis species',0),
 ('NOISE','n/a','Noise / non-bat',0);

INSERT INTO dbo.Sites (SiteCode, SiteName, Easting, Northing, HabitatNotes, ClientRef) VALUES
 ('BEX-01','Bexhill north hedgerow', 573900, 108200, 'Mature hedge, unlit, adjacent arable.', 'CL-2026-014'),
 ('BEX-02','Combe valley woodland edge', 575100, 109050, 'Broadleaf woodland edge over standing water.', 'CL-2026-014'),
 ('HAS-07','Hastings culvert', 581200, 110400, 'Brick culvert, possible lesser horseshoe commuting route.', 'CL-2026-031');

INSERT INTO dbo.Detectors (SerialNumber, Model, FirmwareVer) VALUES
 ('SM4-441207','SM4BAT-FS','2.4.1'), ('SWIFT-0099','Anabat Swift','1.9'), ('SM4-441208','SM4BAT-FS','2.4.1');

INSERT INTO dbo.Deployments (SiteId, DetectorId, StartUtc, EndUtc, SurveyorName, WeatherNotes) VALUES
 (1,1,'2026-05-14T18:00','2026-05-19T06:00','J. Rowe','Dry, 12-14C, light SW breeze.'),
 (2,2,'2026-05-14T18:00','2026-05-19T06:00','J. Rowe','Dry first three nights, rain on night four.'),
 (3,3,'2026-06-02T18:00','2026-06-07T06:00','A. Patel','Warm, still, 16C at dusk.');

INSERT INTO dbo.Recordings (DeploymentId, FileName, RecordedUtc, DurationMs, SampleRateHz, MetaFormXml) VALUES
 (1,'BEX01_20260514_213045.wav','2026-05-14T21:30:45',5000,256000,'<MetaForm><Temp>13.2</Temp></MetaForm>'),
 (1,'BEX01_20260514_221012.wav','2026-05-14T22:10:12',5000,256000,NULL),
 (2,'BEX02_20260515_003300.wav','2026-05-15T00:33:00',3000,384000,'<MetaForm><Temp>11.8</Temp></MetaForm>'),
 (3,'HAS07_20260603_010500.wav','2026-06-03T01:05:00',5000,256000,NULL);

INSERT INTO dbo.Calls (RecordingId, SequenceNo, StartMs, PeakFreqKhz, AutoSpeciesId, AutoConfidence, ManualSpeciesId, ReviewerNotes) VALUES
 (1,1,120,45.20,1,0.9820,1,'Textbook common pip social call at the end of the pass.'),
 (1,2,980,55.10,2,0.7410,2,NULL),
 (2,1,310,19.80,3,0.8890,3,'Noctule, high open flight over the field.'),
 (3,1,450,110.40,4,0.6120,4,'Lesser horseshoe confirmed manually. CEF triggered - do not publish grid ref.'),
 (3,2,1500,32.00,5,0.4200,7,'Auto called barbastelle, actually harmonic noise from the culvert. Classic false positive.'),
 (4,1,220,48.00,6,0.5100,6,'Myotis, not separable to species on this pass.');
GO

PRINT 'NightArc lab fixture ready.';
SELECT (SELECT COUNT(*) FROM dbo.Calls) AS calls, (SELECT COUNT(*) FROM dbo.Sites) AS sites;
GO
