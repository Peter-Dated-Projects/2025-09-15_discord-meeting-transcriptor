import logging
import os

import discord
from discord.ext import commands

from source.context import Context
from source.services.chat.chat_job_manager.attachment_utils import (
    extract_attachments_from_message,
)
from source.services.chat.conversation_manager.in_memory_cache import (
    ConversationStatus,
)

logger = logging.getLogger(__name__)

# Get the context cleaner model from environment
OLLAMA_CONTEXT_CLEANER_MODEL = os.getenv("OLLAMA_CONTEXT_CLEANER_MODEL", "gemma3:12b")


# -------------------------------------------------------------- #
# Cog
# -------------------------------------------------------------- #


class Chat(commands.Cog):
    """Chat-based interaction commands and listeners."""

    def __init__(self, context: Context):
        self.context = context
        # Backward compatibility properties
        self.bot = context.bot
        self.server = context.server_manager
        self.services = context.services_manager

    # -------------------------------------------------------------- #
    # Event Handler Filter
    # -------------------------------------------------------------- #

    async def filter_message(self, message: discord.Message) -> bool:
        """Filter to determine if this cog should handle the message.

        This cog handles messages where:
        - The bot is mentioned, OR
        - The message is in a thread with an active conversation (in-memory or SQL)
        - The message is in an echo-enabled channel/thread
        - In a guild (not DMs)
        - From a non-bot user
        - Thread monitoring is not stopped (unless bot is mentioned to re-enable)
        - Channel is not monitoring reels (reels channels don't accept general LLM queries)

        Args:
            message: The Discord message object

        Returns:
            True if this handler should process the message (pass-through), False otherwise
        """
        # Ignore messages from bots
        if message.author.bot:
            return False

        # Only respond in guilds (not DMs)
        if not message.guild:
            return False

        # Check if bot is mentioned (this always takes priority for some checks)
        bot_mentioned = self.bot.user in message.mentions

        # Get channel ID for echo check
        channel_id = str(message.channel.id)

        # Check if message is in a thread
        if isinstance(message.channel, discord.Thread):
            thread_id = str(message.channel.id)

            # Check if this specific thread is monitoring reels
            # Block ALL LLM queries in reel-monitored threads
            if self.services.instagram_reels_manager.is_channel_monitored(message.channel.id):
                return False

            # Check if this thread is echo-enabled (auto-respond to all messages)
            if self.services.echo_manager.is_echo_enabled(thread_id):
                return True

            # If monitoring is stopped for this thread
            if self.services.conversation_manager.is_monitoring_stopped(thread_id):
                # Only resume if bot is mentioned
                if bot_mentioned:
                    # Resume monitoring
                    self.services.conversation_manager.resume_monitoring(
                        thread_id, self.services.conversations_sql_manager
                    )
                    await self.services.logging_service.info(
                        f"Resumed monitoring thread {thread_id} due to bot mention"
                    )
                    return True
                else:
                    # Don't respond - monitoring is stopped
                    return False

            # Check if conversation is already in memory
            if self.services.conversation_manager.is_conversation_thread(thread_id):
                return True

            # Check if conversation exists in SQL cache
            if self.services.conversation_manager.is_known_thread(thread_id):
                # Try to load the conversation into memory
                try:
                    conversation = await self.services.conversation_manager.load_conversation_from_storage(
                        thread_id=thread_id,
                        conversations_sql_manager=self.services.conversations_sql_manager,
                        conversation_file_manager=self.services.conversation_file_service_manager,
                        conversations_store_sql_manager=self.services.conversations_store_sql_manager,
                    )
                    if conversation:
                        await self.services.logging_service.info(
                            f"Loaded conversation for thread {thread_id} from storage"
                        )
                        return True
                except Exception as e:
                    await self.services.logging_service.error(
                        f"Failed to load conversation for thread {thread_id}: {e}"
                    )

        # Check if this channel is echo-enabled (not a thread, regular channel)
        # This allows responding to all messages in echo-enabled channels
        if self.services.echo_manager.is_echo_enabled(channel_id):
            return True

        # Block bot mentions in reel-monitored channels or threads
        if self.services.instagram_reels_manager.is_channel_monitored(message.channel.id):
            return False

        # Check if the bot is mentioned
        if not bot_mentioned:
            return False

        return True

    # -------------------------------------------------------------- #
    # Event Handlers
    # -------------------------------------------------------------- #

    async def handle_message(self, message: discord.Message) -> bool:
        """Handle messages where the bot is mentioned or in a conversation thread.

        This handler processes:
        1. Messages in existing conversation threads (with or without bot mention)
        2. Bot mentions that create new conversations

        For existing conversations:
        - If conversation is IDLE: creates a new chat job
        - If conversation is THINKING/PROCESSING_QUEUE: queues the message

        For new conversations (bot mention outside thread):
        - Creates a thread from the message
        - Sends "Echo is thinking..." in italics
        - Creates conversation in memory and saves to disk
        - Creates SQL entries for conversation and conversation_store
        - Dispatches a chat job to process the message

        Args:
            message: The Discord message object

        Returns:
            True to pass through to next handler, False to stop propagation
        """

        try:
            # Note: Reel-monitored channel filtering is handled in filter_message()
            # Messages that reach handle_message have already passed all filters
            # No need for redundant checks here

            # Gather message and guild information
            guild_id = str(message.guild.id)
            guild_name = message.guild.name
            channel_id = message.channel.id
            channel_name = message.channel.name if hasattr(message.channel, "name") else "Unknown"
            message_id = message.id
            author_id = str(message.author.id)
            author_name = str(message.author)
            message_content = message.content

            # Extract attachments from the message
            attachments = await extract_attachments_from_message(message)

            # Log attachment extraction
            if attachments:
                await self.services.logging_service.info(
                    f"[ATTACHMENTS] Extracted {len(attachments)} attachments from message {message_id}"
                )
                for i, att in enumerate(attachments, 1):
                    att_type = att.get("type", "unknown")
                    filename = att.get("filename", att.get("url", "unknown"))
                    size = att.get("size")
                    size_str = f" ({size} bytes)" if size else ""
                    await self.services.logging_service.debug(
                        f"[ATTACHMENTS] {i}. {att_type}: {filename}{size_str}"
                    )
            else:
                await self.services.logging_service.debug(
                    f"[ATTACHMENTS] No attachments in message {message_id}"
                )

            # Check if message is in a thread with an active conversation
            if isinstance(message.channel, discord.Thread):
                thread = message.channel
                thread_id = str(thread.id)

                # Check if we have an active conversation for this thread
                conversation = self.services.conversation_manager.get_conversation(thread_id)

                # If not in memory, try to reload from storage.
                # This handles post-restart or idle-eviction scenarios where the conversation
                # was persisted to SQL/disk but no longer lives in the in-memory cache.
                # echo_manager.is_echo_enabled covers threads that had echo auto-enabled when
                # the thread was first created; is_known_thread covers threads loaded into the
                # known_thread_ids cache during the current session.
                if not conversation and (
                    self.services.echo_manager.is_echo_enabled(thread_id)
                    or self.services.conversation_manager.is_known_thread(thread_id)
                ):
                    try:
                        conversation = await self.services.conversation_manager.load_conversation_from_storage(
                            thread_id=thread_id,
                            conversations_sql_manager=self.services.conversations_sql_manager,
                            conversation_file_manager=self.services.conversation_file_service_manager,
                            conversations_store_sql_manager=self.services.conversations_store_sql_manager,
                        )
                        if conversation:
                            await self.services.logging_service.info(
                                f"Reloaded conversation for thread {thread_id} from storage "
                                f"(post-restart / idle-eviction recovery)"
                            )
                        else:
                            await self.services.logging_service.warning(
                                f"Thread {thread_id} is echo-enabled but has no stored conversation to reload"
                            )
                    except Exception as e:
                        await self.services.logging_service.error(
                            f"Failed to reload conversation for thread {thread_id}: {e}"
                        )

                if conversation:
                    # Message in existing conversation thread
                    await self.services.logging_service.info(
                        f"Message in conversation thread '{thread.name}' ({thread_id}) "
                        f"by {author_name} ({author_id})"
                    )
                    await self.services.logging_service.debug(
                        f"Message ID: {message_id}, Content: {message_content[:100]}..."
                    )

                    # Check conversation status
                    if conversation.status == ConversationStatus.IDLE:
                        # Create new chat job
                        conversation_id = await self._get_conversation_id_from_thread(thread_id)
                        if conversation_id:
                            job_id = await self.services.chat_job_manager.create_and_queue_chat_job(
                                thread_id=thread_id,
                                conversation_id=conversation_id,
                                message=message_content,
                                user_id=author_id,
                                attachments=attachments if attachments else None,
                                guild_id=guild_id,
                                discord_message=message,
                            )
                            await self.services.logging_service.info(
                                f"Created chat job {job_id} for existing thread {thread_id}"
                            )
                        else:
                            await self.services.logging_service.error(
                                f"Failed to get conversation ID for thread {thread_id}"
                            )
                    else:
                        # AI is thinking or processing queue - add to message queue
                        queued = await self.services.chat_job_manager.queue_user_message(
                            thread_id=thread_id,
                            message=message_content,
                            user_id=author_id,
                            attachments=attachments if attachments else None,
                        )
                        if queued:
                            await self.services.logging_service.info(
                                f"Queued message from {author_id} in thread {thread_id}"
                            )
                        else:
                            await self.services.logging_service.warning(
                                f"Failed to queue message - no active job for thread {thread_id}"
                            )

                    return True

            # Check if this is an echo-enabled channel (not a thread)
            # Echo-enabled channels allow messages without bot mentions
            if not isinstance(message.channel, discord.Thread):
                channel_id_str = str(channel_id)
                if self.services.echo_manager.is_echo_enabled(channel_id_str):
                    await self.services.logging_service.info(
                        f"Echo-enabled channel message in #{channel_name} ({channel_id_str}) "
                        f"by {author_name} ({author_id})"
                    )

                    # Check if we have an active conversation for this channel
                    conversation = self.services.conversation_manager.get_conversation(
                        channel_id_str
                    )

                    if conversation:
                        # Existing conversation for this channel
                        await self.services.logging_service.info(
                            f"Processing message in echo-enabled channel with existing conversation"
                        )

                        # Check conversation status
                        if conversation.status == ConversationStatus.IDLE:
                            # Create new chat job
                            conversation_id = await self._get_conversation_id_from_thread(
                                channel_id_str
                            )
                            if conversation_id:
                                # Send thinking message
                                await message.channel.send("*Echo is thinking...*")

                                job_id = (
                                    await self.services.chat_job_manager.create_and_queue_chat_job(
                                        thread_id=channel_id_str,
                                        conversation_id=conversation_id,
                                        message=message_content,
                                        user_id=author_id,
                                        attachments=attachments if attachments else None,
                                        guild_id=guild_id,
                                        discord_message=message,
                                    )
                                )
                                await self.services.logging_service.info(
                                    f"Created chat job {job_id} for echo-enabled channel {channel_id_str}"
                                )
                            else:
                                await self.services.logging_service.error(
                                    f"Failed to get conversation ID for echo-enabled channel {channel_id_str}"
                                )
                        else:
                            # AI is thinking or processing queue - add to message queue
                            queued = await self.services.chat_job_manager.queue_user_message(
                                thread_id=channel_id_str,
                                message=message_content,
                                user_id=author_id,
                                attachments=attachments if attachments else None,
                            )
                            if queued:
                                await self.services.logging_service.info(
                                    f"Queued message from {author_id} in echo-enabled channel {channel_id_str}"
                                )
                            else:
                                await self.services.logging_service.warning(
                                    f"Failed to queue message - no active job for channel {channel_id_str}"
                                )

                        return True

                    else:
                        # No conversation yet for this echo-enabled channel - create one
                        await self.services.logging_service.info(
                            f"Creating new conversation for echo-enabled channel #{channel_name} ({channel_id_str})"
                        )

                        # Send thinking message
                        await message.channel.send("*Echo is thinking...*")

                        # Create a new Conversation object using channel_id as the "thread_id"
                        conversation = self.services.conversation_manager.create_conversation(
                            thread_id=channel_id_str,
                            guild_id=guild_id,
                            guild_name=guild_name,
                            requester=author_id,
                        )

                        await self.services.logging_service.info(
                            f"Created conversation in memory for echo-enabled channel {channel_id_str}"
                        )

                        # Save conversation to disk
                        save_success = await conversation.save_conversation()
                        if save_success:
                            await self.services.logging_service.info(
                                f"Saved conversation to disk: {conversation.filename}"
                            )
                        else:
                            await self.services.logging_service.error(
                                f"Failed to save conversation to disk for channel {channel_id_str}"
                            )

                        # Create SQL entry in conversations table
                        conversation_id = (
                            await self.services.conversations_sql_manager.insert_conversation(
                                discord_thread_id=channel_id_str,
                                discord_requester_id=author_id,
                                discord_guild_id=guild_id,
                                chat_meta={
                                    "channel_name": channel_name,
                                    "guild_name": guild_name,
                                    "is_echo_channel": True,
                                },
                            )
                        )

                        await self.services.logging_service.info(
                            f"Created conversation SQL entry: {conversation_id} for echo-enabled channel"
                        )

                        # Create SQL entry in conversation_store table
                        store_id = await self.services.conversations_store_sql_manager.insert_conversation_store(
                            session_id=conversation_id,
                            filename=conversation.filename,
                        )

                        await self.services.logging_service.info(
                            f"Created conversation_store SQL entry: {store_id} for channel {channel_id_str}"
                        )

                        # Create and queue chat job
                        job_id = await self.services.chat_job_manager.create_and_queue_chat_job(
                            thread_id=channel_id_str,
                            conversation_id=conversation_id,
                            message=message_content,
                            user_id=author_id,
                            attachments=attachments if attachments else None,
                            guild_id=guild_id,
                            discord_message=message,
                        )

                        await self.services.logging_service.info(
                            f"Created and queued chat job {job_id} for new echo-enabled channel conversation"
                        )

                        return True

            # If we reach here, it's a bot mention outside of a conversation thread
            # (or in a thread without an active conversation)

            # Verify bot was mentioned
            if self.bot.user not in message.mentions:
                # This shouldn't happen due to filter_message, but handle gracefully
                return True

            # Log the bot mention
            await self.services.logging_service.info(
                f"Bot mentioned in guild '{guild_name}' ({guild_id}) "
                f"by {author_name} ({author_id}) "
                f"in #{channel_name} ({channel_id})"
            )
            await self.services.logging_service.debug(
                f"Message ID: {message_id}, Content: {message_content[:100]}..."
            )

            # Create a thread from the message or use existing thread
            thread_name = f"Chat with {message.author.name}"

            if isinstance(message.channel, discord.Thread):
                # Already in a thread - check if conversation exists in SQL
                thread = message.channel
                thread_id = str(thread.id)

                # Check if this thread already has a SQL entry (regardless of user)
                existing_conversation_id = await self._get_conversation_id_from_thread(thread_id)

                if existing_conversation_id:
                    # Thread already has a conversation - load it into memory
                    await self.services.logging_service.info(
                        f"Thread {thread_id} already has conversation {existing_conversation_id}, loading into memory"
                    )

                    try:
                        conversation = await self.services.conversation_manager.load_conversation_from_storage(
                            thread_id=thread_id,
                            conversations_sql_manager=self.services.conversations_sql_manager,
                            conversation_file_manager=self.services.conversation_file_service_manager,
                            conversations_store_sql_manager=self.services.conversations_store_sql_manager,
                        )

                        if conversation:
                            await self.services.logging_service.info(
                                f"Loaded existing conversation for thread {thread_id} from storage"
                            )

                            # Auto-enable echo for this thread so all messages are processed
                            if not self.services.echo_manager.is_echo_enabled(thread_id):
                                await self.services.echo_manager.enable_echo(
                                    channel_id=thread_id,
                                    guild_id=guild_id,
                                    echo_sql_manager=self.services.echo_sql_manager,
                                )
                                await self.services.logging_service.info(
                                    f"Auto-enabled echo for existing thread {thread_id}"
                                )

                            # Create and queue chat job with existing conversation
                            job_id = await self.services.chat_job_manager.create_and_queue_chat_job(
                                thread_id=thread_id,
                                conversation_id=existing_conversation_id,
                                message=message_content,
                                user_id=author_id,
                                attachments=attachments if attachments else None,
                                guild_id=guild_id,
                                discord_message=message,
                            )

                            await self.services.logging_service.info(
                                f"Created and queued chat job {job_id} for existing conversation in thread {thread_id}"
                            )

                            return True
                        else:
                            await self.services.logging_service.warning(
                                f"Failed to load conversation for thread {thread_id}, creating new one"
                            )
                    except Exception as e:
                        await self.services.logging_service.error(
                            f"Error loading conversation for thread {thread_id}: {e}, creating new one"
                        )

                # No existing conversation in SQL for this thread
                await self.services.logging_service.info(
                    f"Creating new conversation in existing thread: {thread.name} ({thread_id})"
                )

                # Auto-enable echo for this existing thread so all messages are processed
                if not self.services.echo_manager.is_echo_enabled(thread_id):
                    await self.services.echo_manager.enable_echo(
                        channel_id=thread_id,
                        guild_id=guild_id,
                        echo_sql_manager=self.services.echo_sql_manager,
                    )
                    await self.services.logging_service.info(
                        f"Auto-enabled echo for existing thread {thread_id}"
                    )
            else:
                # Create a new thread from the message
                thread = await message.create_thread(
                    name=thread_name, auto_archive_duration=60  # Archive after 1 hour of inactivity
                )
                thread_id = str(thread.id)
                await self.services.logging_service.info(
                    f"Created thread: {thread.name} ({thread_id})"
                )

                # Auto-enable echo for the new thread so all messages are processed
                await self.services.echo_manager.enable_echo(
                    channel_id=thread_id,
                    guild_id=guild_id,
                    echo_sql_manager=self.services.echo_sql_manager,
                )
                await self.services.logging_service.info(
                    f"Auto-enabled echo for new thread {thread_id}"
                )

            # Send "Echo is thinking..." message in the thread (italicized)
            await thread.send("*Echo is thinking...*")
            await self.services.logging_service.info(f"Sent thinking message in thread {thread_id}")

            # Create a new Conversation object
            conversation = self.services.conversation_manager.create_conversation(
                thread_id=thread_id,
                guild_id=guild_id,
                guild_name=guild_name,
                requester=author_id,
            )

            await self.services.logging_service.info(
                f"Created conversation in memory for thread {thread_id}"
            )

            # Save conversation to disk
            save_success = await conversation.save_conversation()
            if save_success:
                await self.services.logging_service.info(
                    f"Saved conversation to disk: {conversation.filename}"
                )
            else:
                await self.services.logging_service.error(
                    f"Failed to save conversation to disk for thread {thread_id}"
                )

            # Create SQL entry in conversations table
            conversation_id = await self.services.conversations_sql_manager.insert_conversation(
                discord_thread_id=thread_id,
                discord_requester_id=author_id,
                discord_guild_id=guild_id,
                chat_meta={"thread_name": thread_name, "guild_name": guild_name},
            )

            await self.services.logging_service.info(
                f"Created conversation SQL entry: {conversation_id}"
            )

            # Create SQL entry in conversations_store table
            store_id = (
                await self.services.conversations_store_sql_manager.insert_conversation_store(
                    session_id=conversation_id, filename=conversation.filename
                )
            )

            await self.services.logging_service.info(
                f"Created conversation store SQL entry: {store_id}"
            )

            # Create and queue chat job
            job_id = await self.services.chat_job_manager.create_and_queue_chat_job(
                thread_id=thread_id,
                conversation_id=conversation_id,
                message=message_content,
                user_id=author_id,
                attachments=attachments if attachments else None,
                guild_id=guild_id,
                discord_message=message,
            )

            await self.services.logging_service.info(
                f"Created and queued chat job {job_id} for thread {thread_id}"
            )

            # Return True to allow pass-through to next handler
            return True

        except discord.Forbidden:
            await self.services.logging_service.error(
                f"Missing permissions to create thread or send message in guild {message.guild.id}"
            )
            # Return True to allow other handlers to attempt processing
            return True
        except discord.HTTPException as e:
            await self.services.logging_service.error(
                f"HTTP error while handling message {message.id}: {e}"
            )
            # Return True to allow other handlers to attempt processing
            return True
        except Exception as e:
            await self.services.logging_service.error(
                f"Unexpected error handling bot mention: {e}", exc_info=True
            )
            # Return True to allow other handlers to attempt processing
            return True

    async def _get_conversation_id_from_thread(self, thread_id: str) -> str | None:
        """
        Get conversation ID from thread ID by querying SQL.

        Args:
            thread_id: Discord thread ID

        Returns:
            Conversation ID or None if not found
        """
        try:
            conversation = (
                await self.services.conversations_sql_manager.retrieve_conversation_by_thread_id(
                    thread_id
                )
            )
            if conversation:
                return conversation.get("id")
            return None
        except Exception as e:
            await self.services.logging_service.error(
                f"Failed to get conversation ID for thread {thread_id}: {e}"
            )
            return None

    # -------------------------------------------------------------- #
    # Slash Commands
    # -------------------------------------------------------------- #

    @commands.slash_command(
        name="stop-monitoring-channel",
        description="Stop the bot from monitoring this conversation thread",
    )
    async def stop_monitoring_channel(self, ctx: discord.ApplicationContext):
        """Stop monitoring the current conversation thread.

        The bot will no longer respond to messages in this thread unless
        it is mentioned again. This is useful when the user is done with
        the conversation and wants to end the interaction.
        """
        # Log command invocation
        await self.services.logging_service.info(
            f"User {ctx.author.id} ({ctx.author.name}) requested stop-monitoring-channel in channel {ctx.channel_id}"
        )

        # Defer response
        await ctx.defer(ephemeral=True)

        try:
            # Check if we're in a thread
            if not isinstance(ctx.channel, discord.Thread):
                embed = discord.Embed(
                    title="❌ Not in a Thread",
                    description="This command can only be used in a conversation thread.",
                    color=discord.Color.red(),
                )
                await ctx.followup.send(embed=embed, ephemeral=True)
                return

            thread_id = str(ctx.channel.id)

            # Check if this thread has a conversation (in memory or SQL)
            has_conversation = self.services.conversation_manager.is_conversation_thread(
                thread_id
            ) or self.services.conversation_manager.is_known_thread(thread_id)

            if not has_conversation:
                embed = discord.Embed(
                    title="❌ No Conversation Found",
                    description="This thread does not have an active conversation to stop monitoring.",
                    color=discord.Color.red(),
                )
                await ctx.followup.send(embed=embed, ephemeral=True)
                return

            # Check if already stopped
            if self.services.conversation_manager.is_monitoring_stopped(thread_id):
                embed = discord.Embed(
                    title="ℹ️ Already Stopped",
                    description="Monitoring for this thread is already stopped. Mention the bot to resume.",
                    color=discord.Color.blue(),
                )
                await ctx.followup.send(embed=embed, ephemeral=True)
                return

            # Stop monitoring
            was_monitoring = self.services.conversation_manager.stop_monitoring(
                thread_id, self.services.conversations_sql_manager
            )

            if was_monitoring:
                await self.services.logging_service.info(
                    f"Stopped monitoring thread {thread_id} via command by user {ctx.author.id}"
                )

                # Also disable echo for this thread if enabled
                if self.services.echo_manager.is_echo_enabled(thread_id):
                    await self.services.echo_manager.disable_echo(
                        thread_id, self.services.echo_sql_manager
                    )
                    await self.services.logging_service.info(
                        f"Disabled echo for thread {thread_id} due to stop-monitoring-channel"
                    )

                # Send confirmation message FIRST
                embed = discord.Embed(
                    title="✅ Monitoring Stopped",
                    description=(
                        "The bot will no longer respond to messages in this thread.\n\n"
                        "To resume the conversation, simply mention the bot."
                    ),
                    color=discord.Color.green(),
                )
                await ctx.followup.send(embed=embed, ephemeral=True)

                # Run context cleaning workflow AFTER sending confirmation
                try:
                    conversation = self.services.conversation_manager.get_conversation(thread_id)
                    if conversation:
                        # Import here to avoid circular imports
                        from source.services.chat.mcp.subroutine_manager.subroutines.context_cleaning import (
                            ContextCleaningSubroutine,
                        )

                        await self.services.logging_service.info(
                            f"Running context cleaning for thread {thread_id} after stopping monitoring via command"
                        )

                        # Create and run the context cleaning subroutine
                        subroutine = ContextCleaningSubroutine(
                            ollama_request_manager=self.services.ollama_request_manager,
                            conversation=conversation,
                            model=OLLAMA_CONTEXT_CLEANER_MODEL,
                            logging_service=self.services.logging_service,
                        )

                        await subroutine.ainvoke({"messages": []})

                        # Save the updated conversation
                        await conversation.save_conversation()

                        await self.services.logging_service.info(
                            f"Context cleaning completed for thread {thread_id}"
                        )
                except Exception as e:
                    # Log the error but don't fail the stop monitoring operation
                    await self.services.logging_service.error(
                        f"Failed to run context cleaning after stop-monitoring-channel: {str(e)}"
                    )
            else:
                embed = discord.Embed(
                    title="❌ Failed to Stop Monitoring",
                    description="This thread was not being monitored or does not exist.",
                    color=discord.Color.red(),
                )
                await ctx.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await self.services.logging_service.error(
                f"Error in stop-monitoring-channel command: {e}", exc_info=True
            )
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred while stopping monitoring: {str(e)}",
                color=discord.Color.red(),
            )
            await ctx.followup.send(embed=embed, ephemeral=True)

    @commands.slash_command(
        name="echo_enable",
        description="Enable echo bot interaction in this channel",
    )
    async def echo_enable(self, ctx: discord.ApplicationContext):
        """Enable echo bot in this channel.

        The bot will respond to all messages in this channel while echo is enabled.
        Context is maintained per-channel and persists across messages.

        This command can only be used in message channels, not in threads.
        """
        # Log command invocation
        await self.services.logging_service.info(
            f"User {ctx.author.id} ({ctx.author.name}) requested echo_enable in channel {ctx.channel_id}"
        )

        # Defer response
        await ctx.defer(ephemeral=True)

        try:
            # Check if in a thread - block execution
            if isinstance(ctx.channel, discord.Thread):
                embed = discord.Embed(
                    title="❌ Cannot Use in Thread",
                    description=(
                        "This command can only be used in message channels, not threads.\n\n"
                        "Please run this command in a regular text channel."
                    ),
                    color=discord.Color.red(),
                )
                await ctx.followup.send(embed=embed, ephemeral=True)
                return

            channel_id = str(ctx.channel.id)
            guild_id = str(ctx.guild.id)

            # Check if already enabled
            if self.services.echo_manager.is_echo_enabled(channel_id):
                embed = discord.Embed(
                    title="ℹ️ Already Enabled",
                    description=(
                        "Echo bot is already active in this channel.\n\n"
                        "Use `/echo_disable` to stop echo interaction."
                    ),
                    color=discord.Color.blue(),
                )
                await ctx.followup.send(embed=embed, ephemeral=True)
                return

            # Enable echo
            success = await self.services.echo_manager.enable_echo(
                channel_id, guild_id, self.services.echo_sql_manager
            )

            if success:
                await self.services.logging_service.info(
                    f"Echo enabled for channel {channel_id} in guild {guild_id} by user {ctx.author.id}"
                )

                embed = discord.Embed(
                    title="✅ Echo Enabled",
                    description=(
                        "Echo bot is now active in this channel.\n\n"
                        "The bot will respond to all messages here.\n"
                        "Context will be maintained across messages.\n\n"
                        "Use `/echo_disable` to stop and clear context."
                    ),
                    color=discord.Color.green(),
                )
                await ctx.followup.send(embed=embed, ephemeral=True)
            else:
                embed = discord.Embed(
                    title="❌ Failed to Enable",
                    description="Failed to enable echo for this channel.",
                    color=discord.Color.red(),
                )
                await ctx.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await self.services.logging_service.error(
                f"Error in echo_enable command: {e}", exc_info=True
            )
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred while enabling echo: {str(e)}",
                color=discord.Color.red(),
            )
            await ctx.followup.send(embed=embed, ephemeral=True)

    @commands.slash_command(
        name="echo_disable",
        description="Disable echo bot interaction in this channel",
    )
    async def echo_disable(self, ctx: discord.ApplicationContext):
        """Disable echo bot in this channel.

        The bot will stop responding to messages in this channel.
        All conversation context will be cleared and messages during
        the disabled period will not be remembered.

        This command can only be used in message channels, not in threads.
        For threads, use /stop-monitoring-channel instead.
        """
        # Log command invocation
        await self.services.logging_service.info(
            f"User {ctx.author.id} ({ctx.author.name}) requested echo_disable in channel {ctx.channel_id}"
        )

        # Defer response
        await ctx.defer(ephemeral=True)

        try:
            # Check if in a thread - block execution
            if isinstance(ctx.channel, discord.Thread):
                embed = discord.Embed(
                    title="❌ Cannot Use in Thread",
                    description=(
                        "This command can only be used in message channels, not threads.\n\n"
                        "To stop the bot from responding in this thread, use `/stop-monitoring-channel` instead."
                    ),
                    color=discord.Color.red(),
                )
                await ctx.followup.send(embed=embed, ephemeral=True)
                return

            channel_id = str(ctx.channel.id)

            # Check if echo is enabled
            if not self.services.echo_manager.is_echo_enabled(channel_id):
                embed = discord.Embed(
                    title="ℹ️ Not Enabled",
                    description=(
                        "Echo bot is not active in this channel.\n\n"
                        "Use `/echo_enable` to start echo interaction."
                    ),
                    color=discord.Color.blue(),
                )
                await ctx.followup.send(embed=embed, ephemeral=True)
                return

            # Disable echo (this also clears context)
            success = await self.services.echo_manager.disable_echo(
                channel_id, self.services.echo_sql_manager
            )

            if success:
                await self.services.logging_service.info(
                    f"Echo disabled for channel {channel_id} by user {ctx.author.id}"
                )

                embed = discord.Embed(
                    title="✅ Echo Disabled",
                    description=(
                        "Echo bot has been disabled in this channel.\n\n"
                        "All conversation context has been cleared.\n"
                        "Messages during the disabled period will not be remembered.\n\n"
                        "Use `/echo_enable` to start a new conversation."
                    ),
                    color=discord.Color.green(),
                )
                await ctx.followup.send(embed=embed, ephemeral=True)
            else:
                embed = discord.Embed(
                    title="❌ Failed to Disable",
                    description="Failed to disable echo for this channel.",
                    color=discord.Color.red(),
                )
                await ctx.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await self.services.logging_service.error(
                f"Error in echo_disable command: {e}", exc_info=True
            )
            embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred while disabling echo: {str(e)}",
                color=discord.Color.red(),
            )
            await ctx.followup.send(embed=embed, ephemeral=True)


def setup(context: Context):
    """Setup function for the Chat cog.

    Args:
        context: The application context instance

    Returns:
        The initialized Chat cog instance
    """
    chat = Chat(context)
    context.bot.add_cog(chat)
    return chat
