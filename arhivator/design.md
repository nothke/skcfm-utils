# Archive Explorer

- A GUI tool for playing and snipping archive audio clips
- Archive clips are in a folder stored as one hour wav files, but will not necessarily be wavs in the future
- The clips start datetime is at last modified date - length of clip
- The tool should have playback segment, where there is an option to play the clip at selected date/time. This would load the approapriate clip from the archive and play it.
- Dates should be easily copy-pastable in the form "D.M.Y. H:M:S:MS", so "12.5.2026. 10:15:01:001"
- Each date entry should also have a "..." button that opens the calendar dialogue
- There should be a snip segment, where you can designate a from and to and the tool will trim and combine the clips and create a new wav (or mp3 if the user desires)
- You can give the tool multiple archive folders as input
- In the snip segment there should also be a play button that should play starting at snip section
- Selecting and editing time blocks
- Linux and windows run.bat and run.sh that automatically creates python env and installs correct python libs

## Time blocks
Time blocks are time segments (they have start and end), and are stored in a global time_blocks.json file. They represent radio show episodes. Each timeblock should have these parameters:
- name (string)
- start_time (time in "D.M.Y. H:M:S:MS" form)
- end_time (same as above)
- episode_num (integer)
- description (string)
- hosts (array of strings)
- guests (array of strings)

Time blocks should be visible as rectangles with a name in the interface, see Interface section

## Interface
The main interface consists of two parts:
- The timeline/coverage viewport, upper part of the window
    - in the upper part, when zoomed out it should show the coverage, when zoomed in it should show the envelope
    - in the lower part it should show all time blocks, that can be selected.
        - They are rendered as rectangles with a name over them
        - If there are overlapping time blocks in view, they should be drawn in separate rows
        - Right click on a time block and there should be options:
            - set snip range to block (sets start and end of snip range to the corresponding block)
            - set block to range (sets the size of that block to the selected range)
    - the bottom strip should have time measurement ticks in 2 layers:
        - the upper strip should scale with the zoom level (as it already exists)
        - the lower part is similar to upper but it doesn't scale, it only shows the date in the form "D.M.Y. DAY_IN_WEEK" so e.g. "5.3.2025. Wednesday"

- "archive folders" button opens a dialog window where you can set archive folders

- The controls, lower part of the window, split in two columns:
    - left column is the cursor and snipping controls
        - UI that shows current cursor datetime in a text window (that can be selected and copied)
        - play button (plays from wherever the cursor is)
        - segment start and end datetime
        - export button (opens "save as" dialog that exports a .wav file from the selected segment)
    - right column is the time blocks editor, where each parameter can be input and saved with a button
        
## Keys:
- scroll on waveform, zoom in (done)
- left click drag - select region (done)
- right or middle click drag - pan (done)
- click on waveform, puts cursor there (done)
- click on time block, selects it and sets for editing (done)
- delete, deletes selected time block (with confirm window Are you sure you want to delete NAME?)
- right click opens context menu
- i - set region start
- o - set region end

## Right click context menu:
- On clip viewport:
    - play from here
    - ----- separator ------
    - set region start
    - set region end
    - new time block (if region exists)
    - ----- separator ------
    - (if selected block) set selected block start
    - (if selected block) set selected block end
- On time block:
    - edit time block
    - set selected region to block
    - set block to selected region (if region is non-zero)
    - delete time block (shows confirm window)

## Next steps:
- "Play selection" and normal play are inconsistent. When playing selection the top Play button still thinks it's not playing. Instead, "Play selection" should behave exactly like normal play, the only difference is that it first puts playhead on the start of the selection
- Add fade in and out to export, 0.1s should be default
- Reword BEXT export
- Final UI adjustments:
    - Add export to a separate group "Export"
    - Split export params into multiple rows
    - Leave play selection button in region selection and move "Export region.." button to "Export"

## After that:
- Move this to fm tools repo